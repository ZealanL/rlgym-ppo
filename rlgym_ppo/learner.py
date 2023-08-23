"""
File: learner.py
Author: Matthew Allen

Description:
The primary algorithm file. The Learner object coordinates timesteps from the workers 
and sends them to PPO, keeps track
of the misc. variables and statistics for logging, reports to wandb and the console, 
and handles checkpointing.
"""

import json
import os
import random
import shutil
import time
from typing import Union

import numpy as np
import torch
import wandb
from rlgym_sim import gym

from rlgym_ppo.batched_agents import BatchedAgentManager
from rlgym_ppo.ppo import ExperienceBuffer, PPOLearner
from rlgym_ppo.util import WelfordRunningStat, reporting, torch_functions


class Learner(object):
    def __init__(
        # fmt: off
        self,
        env_create_function: gym.Gym,
        n_proc: int = 8,
        min_inference_size: int = 16,
        render: bool = False,
        render_delay: float = 0,

        timestep_limit: int = 5_000_000_000,
        exp_buffer_size: int = 500,
        ts_per_iteration: int = 500,

        policy_layer_sizes: tuple[int,...] = (256, 256, 256),
        critic_layer_sizes: tuple[int,...] = (256, 256, 256),
        continuous_var_range: tuple[float,...] = (0.1, 1.0),

        ppo_epochs: int = 10,
        ppo_batch_size: int = 500,
        ppo_ent_coef: float = 0.01,
        ppo_clip_range: float = 0.2,

        gae_lambda: float = 0.95,
        gae_gamma: float = 0.99,
        policy_lr: float = 3e-4,
        critic_lr: float = 3e-4,

        log_to_wandb: bool = False,
        load_wandb: bool = True,
        wandb_run: Union[str,None] = None,
        wandb_project_name: Union[str,None] = None,
        wandb_group_name: Union[str,None] = None,
        wandb_run_name: Union[str,None] = None,

        checkpoints_save_folder: Union[str,None] = None,
        add_unix_timestamp: bool = True,
        checkpoint_load_folder: Union[str,None] = None,
        save_every_ts: int = 1_000_000,

        instance_launch_delay: Union[float,None] = None,
        random_seed: int = 123,
        n_checkpoints_to_keep: int = 5,
        device: str = "auto"
        # fmt: on
    ):
        assert (
            env_create_function is not None
        ), "MUST PROVIDE A FUNCTION TO CREATE RLGYM FUNCTIONS TO INITIALIZE RLGYM-PPO"

        if checkpoints_save_folder is None:
            checkpoints_save_folder = os.path.join(
                "data", "checkpoints", "rlgym-ppo-run"
            )

        # Add the option for the user to turn off the addition of Unix Timestamps to
        # the ``checkpoints_save_folder`` path
        if add_unix_timestamp:
            checkpoints_save_folder = f"{checkpoints_save_folder}-{time.time_ns()}"

        torch.manual_seed(random_seed)
        np.random.seed(random_seed)
        random.seed(random_seed)

        self.n_checkpoints_to_keep = n_checkpoints_to_keep
        self.checkpoints_save_folder = checkpoints_save_folder
        self.save_every_ts = save_every_ts

        if device in {"auto", "gpu"} and torch.cuda.is_available():
            self.device = "cuda:0"
        elif device == "auto" and not torch.cuda.is_available():
            self.device = "cpu"
        else:
            self.device = device

        print(f"Using device {self.device}")
        self.exp_buffer_size = exp_buffer_size
        self.timestep_limit = timestep_limit
        self.ts_per_epoch = ts_per_iteration
        self.gae_lambda = gae_lambda
        self.gae_gamma = gae_gamma
        self.return_stats = WelfordRunningStat(1)
        self.epoch = 0

        self.experience_buffer = ExperienceBuffer(
            self.exp_buffer_size, seed=random_seed, device=self.device
        )

        print("Initializing processes...")
        self.agent = BatchedAgentManager(
            None, min_inference_size=min_inference_size, seed=random_seed
        )
        obs_space_size, act_space_size, action_space_type = self.agent.init_processes(
            n_processes=n_proc,
            build_env_fn=env_create_function,
            spawn_delay=instance_launch_delay,
            render=render,
            render_delay=render_delay,
        )
        obs_space_size = np.prod(obs_space_size)
        print("Initializing PPO...")
        self.ppo_learner = PPOLearner(
            obs_space_size,
            act_space_size,
            device=self.device,
            batch_size=ppo_batch_size,
            n_epochs=ppo_epochs,
            continuous_var_range=continuous_var_range,
            policy_type=action_space_type,
            policy_layer_sizes=policy_layer_sizes,
            critic_layer_sizes=critic_layer_sizes,
            policy_lr=policy_lr,
            critic_lr=critic_lr,
            clip_range=ppo_clip_range,
            ent_coef=ppo_ent_coef,
        )

        self.agent.policy = self.ppo_learner.policy

        self.wandb_run = wandb_run
        wandb_loaded = checkpoint_load_folder is not None and self.load(
            checkpoint_load_folder, load_wandb
        )

        if log_to_wandb and self.wandb_run is None and not wandb_loaded:
            project = "rlgym-ppo" if wandb_project_name is None else wandb_project_name
            group = "unnamed-runs" if wandb_group_name is None else wandb_group_name
            run_name = "rlgym-ppo-run" if wandb_run_name is None else wandb_run_name
            print("Attempting to create new wandb run...")
            self.wandb_run = wandb.init(
                project=project, group=group, name=run_name, reinit=True
            )
            print("Created new wandb run!", self.wandb_run.id)
        print("Learner successfully initialized!")

    def learn(self):
        """
        Function to wrap the _learn function in a try/catch/finally
        block to ensure safe execution and error handling.
        :return: None
        """
        try:
            self._learn()
        except Exception:
            import traceback

            print("\n\nLEARNING LOOP ENCOUNTERED AN ERROR\n")
            traceback.print_exc()
        finally:
            self.cleanup()

    def _learn(self):
        """
        Learning function. This is where the magic happens.
        :return: None
        """

        # While the number of timesteps we have collected so far is less than the
        # amount we are allowed to collect.
        while self.agent.cumulative_timesteps < self.timestep_limit:
            epoch_start = time.perf_counter()
            report = {}

            # Collect the desired number of timesteps from our agent.
            experience, steps_collected, collection_time = self.agent.collect_timesteps(
                self.ts_per_epoch
            )

            # Add the new experience to our buffer and compute the various
            # reinforcement learning quantities we need to
            # learn from (advantages, values, returns).
            self.add_new_experience(experience)

            # Let PPO compute updates using our experience buffer.
            ppo_report = self.ppo_learner.learn(self.experience_buffer)
            epoch_stop = time.perf_counter()
            epoch_time = epoch_stop - epoch_start

            # Report variables we care about.
            report |= ppo_report
            if self.epoch < 1:
                report["Value Function Loss"] = np.nan

            report["Cumulative Timesteps"] = self.agent.cumulative_timesteps
            report["Total Iteration Time"] = epoch_time
            report["Timesteps Collected"] = steps_collected
            report["Timestep Collection Time"] = collection_time
            report["Timestep Consumption Time"] = epoch_time - collection_time
            report["Collected Steps per Second"] = steps_collected / collection_time
            report["Overall Steps per Second"] = steps_collected / epoch_time
            if self.agent.average_reward is not None:
                report["Policy Reward"] = self.agent.average_reward
            else:
                report["Policy Reward"] = np.nan

            # Log to wandb and print to the console.
            reporting.report_metrics(
                loggable_metrics=report, debug_metrics=None, wandb_run=self.wandb_run
            )

            report.clear()
            ppo_report.clear()

            # Save if we've reached the next checkpoint timestep.
            if self.epoch * self.ts_per_epoch % self.save_every_ts == 0:
                self.save(int(round(self.epoch * self.ts_per_epoch)))

            self.epoch += 1
        self.cleanup()

    @torch.no_grad()
    def add_new_experience(self, experience):
        """
        Function to add timesteps to our experience buffer and compute the advantage
        function estimates, value function
        estimates, and returns.
        :param experience: tuple containing
        (experience, steps_collected, collection_time) from an agent.
        :return: None
        """

        # Unpack timestep data.
        states, actions, log_probs, rewards, next_states, dones, truncated = experience
        value_net = self.ppo_learner.value_net

        # Construct input to the value function estimator that includes the final state
        # (which an action was not taken in)
        val_inp = np.zeros(shape=(states.shape[0] + 1, states.shape[1]))
        val_inp[:-1] = states
        val_inp[-1] = next_states[-1]

        # Predict the expected returns at each state.
        val_preds = value_net(val_inp).flatten().tolist()

        # Compute the desired reinforcement learning quantities.
        value_targets, advantages, returns = torch_functions.compute_gae(
            rewards,
            dones,
            truncated,
            val_preds,
            gamma=self.gae_gamma,
            lmbda=self.gae_lambda,
            return_std=self.return_stats.std[0],
        )

        # Update the running statistics about the returns.
        self.return_stats.increment(returns, len(returns))

        # Add our new experience to the buffer.
        self.experience_buffer.submit_experience(
            states,
            actions,
            log_probs,
            rewards,
            next_states,
            dones,
            truncated,
            value_targets,
            advantages,
        )

    def save(self, cumulative_timesteps):
        """
        Function to save a checkpoint.
        :param cumulative_timesteps: Number of timesteps that have passed so far in the
        learning algorithm.
        :return: None
        """

        # Make the file path to which the checkpoint will be saved
        folder_path = os.path.join(
            self.checkpoints_save_folder, str(cumulative_timesteps)
        )
        os.makedirs(folder_path, exist_ok=True)

        # Check to see if we've run out of checkpoint space and remove the oldest
        # checkpoints
        print(f"Saving checkpoint {cumulative_timesteps}...")
        existing_checkpoints = [
            int(arg) for arg in os.listdir(self.checkpoints_save_folder)
        ]
        if len(existing_checkpoints) > self.n_checkpoints_to_keep:
            existing_checkpoints.sort()
            for checkpoint_name in existing_checkpoints[: -self.n_checkpoints_to_keep]:
                shutil.rmtree(
                    os.path.join(self.checkpoints_save_folder, str(checkpoint_name))
                )

        os.makedirs(folder_path, exist_ok=True)

        # Save all the things that need saving.
        self.ppo_learner.save_to(folder_path)
        book_keeping_vars = {
            "cumulative_timesteps": self.agent.cumulative_timesteps,
            "cumulative_model_updates": self.ppo_learner.cumulative_model_updates,
            "policy_average_reward": self.agent.average_reward,
            "epoch": self.epoch,
            "running_stats": self.return_stats.to_json(),
        }

        if self.wandb_run is not None:
            book_keeping_vars["wandb_run_id"] = self.wandb_run.id
            book_keeping_vars["wandb_project"] = self.wandb_run.project
            book_keeping_vars["wandb_entity"] = self.wandb_run.entity
            book_keeping_vars["wandb_group"] = self.wandb_run.group

        book_keeping_table_path = os.path.join(folder_path, "BOOK_KEEPING_VARS.json")
        with open(book_keeping_table_path, "w") as f:
            json.dump(book_keeping_vars, f, indent=4)

        print(f"Checkpoint {cumulative_timesteps} saved!\n")

    def load(self, folder_path, load_wandb):
        """
        Function to load the learning algorithm from a checkpoint.

        :param folder_path: Path to the checkpoint folder that will be loaded.
        :param load_wandb: Whether to resume an existing weights and biases run that
        was saved with the checkpoint being loaded.
        :return: None
        """

        # Make sure the folder exists.
        assert os.path.exists(folder_path), f"UNABLE TO LOCATE FOLDER {folder_path}"
        print(f"Loading from checkpoint at {folder_path}")

        # Load stuff.
        self.ppo_learner.load_from(folder_path)

        wandb_loaded = False
        with open(os.path.join(folder_path, "BOOK_KEEPING_VARS.json"), "r") as f:
            book_keeping_vars = dict(json.load(f))
            self.agent.cumulative_timesteps = book_keeping_vars["cumulative_timesteps"]
            self.agent.average_reward = book_keeping_vars["policy_average_reward"]
            self.ppo_learner.cumulative_model_updates = book_keeping_vars[
                "cumulative_model_updates"
            ]
            self.return_stats.from_json(book_keeping_vars["running_stats"])
            self.epoch = book_keeping_vars["epoch"]
            if "wandb_run_id" in book_keeping_vars and load_wandb:
                self.wandb_run = wandb.init(
                    settings=wandb.Settings(start_method="spawn"),
                    entity=book_keeping_vars["wandb_entity"],
                    project=book_keeping_vars["wandb_project"],
                    group=book_keeping_vars["wandb_group"],
                    id=book_keeping_vars["wandb_run_id"],
                    resume="allow",
                    reinit=True,
                )
                wandb_loaded = True

        print("Checkpoint loaded!")
        return wandb_loaded

    def cleanup(self):
        """
        Function to clean everything up before shutting down.
        :return: None.
        """

        if self.wandb_run is not None:
            self.wandb_run.finish()
        if type(self.agent) == BatchedAgentManager:
            self.agent.cleanup()
        self.experience_buffer.clear()
