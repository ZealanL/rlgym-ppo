"""
File: experience_buffer.py
Author: Matthew Allen

Description:
    A buffer containing the experience to be learned from on this iteration. The buffer may be added to, removed from,
    and shuffled. When the maximum specified size of the buffer is exceeded, the least recent entries will be removed in
    a FIFO fashion.
"""

import numpy as np
import torch


class ExperienceBuffer(object):
    def __init__(self, max_size, seed, device):
        self.device = device
        self.seed = seed
        self.states = torch.FloatTensor().to(self.device)
        self.actions = torch.FloatTensor().to(self.device)
        self.log_probs = torch.FloatTensor().to(self.device)
        self.rewards = torch.FloatTensor().to(self.device)
        self.next_states = torch.FloatTensor().to(self.device)
        self.dones = torch.FloatTensor().to(self.device)
        self.truncated = torch.FloatTensor().to(self.device)
        self.values = torch.FloatTensor().to(self.device)
        self.advantages = torch.FloatTensor().to(self.device)
        self.max_size = max_size
        self.rng = np.random.RandomState(seed)

    def submit_experience(self, states, actions, log_probs, rewards, next_states, dones, truncated, values, advantages):
        """
        Function to add experience to the buffer.

        :param states: An ordered sequence of states from the environment.
        :param actions: The corresponding actions that were taken at each state in the `states` sequence.
        :param log_probs: The log probability for each action in `actions`
        :param rewards: A list rewards for each pair in `states` and `actions`
        :param next_states: An ordered sequence of next states (the states which occurred after an action) from the environment.
        :param dones: An ordered sequence of the done (terminated) flag from the environment.
        :param truncated: An ordered sequence of the truncated flag from the environment.
        :param values: The output of the value function estimator evaluated on the concatenation of `states` and the final state in `next_states`
        :param advantages: The advantage of each action at each state in `states` and `actions`

        :return: None
        """

        self.states = torch.cat((self.states, torch.as_tensor(states, dtype=torch.float32, device=self.device)), 0)
        self.actions = torch.cat((self.actions, torch.as_tensor(actions, dtype=torch.float32, device=self.device)), 0)
        self.log_probs = torch.cat((self.log_probs, torch.as_tensor(log_probs, dtype=torch.float32, device=self.device)), 0)
        self.rewards = torch.cat((self.rewards, torch.as_tensor(rewards, dtype=torch.float32, device=self.device)), 0)
        self.next_states = torch.cat((self.next_states, torch.as_tensor(next_states, dtype=torch.float32, device=self.device)), 0)
        self.dones = torch.cat((self.dones, torch.as_tensor(dones, dtype=torch.float32, device=self.device)), 0)
        self.truncated = torch.cat((self.truncated, torch.as_tensor(truncated, dtype=torch.float32, device=self.device)), 0)
        self.values = torch.cat((self.values, torch.as_tensor(values, dtype=torch.float32, device=self.device)), 0)
        self.advantages = torch.cat((self.advantages, torch.as_tensor(advantages, dtype=torch.float32, device=self.device)), 0)
        self._clamp_size()

    def get_all_batches_shuffled(self, batch_size):
        """
        Function to shuffle all the data in the experience buffer, split it into batches, and return those.

        :param batch_size: The size of each batch.
        :return: Array containing the shuffled batches.
        """

        # A list of indices for each entry in our experience buffer.
        indices = [i for i in range(self.rewards.shape[0])]

        # Shuffle all the indices.
        self.rng.shuffle(indices)

        # Access the data we are going to shuffle.
        acts, probs, rews, obs, next_obs, vals, adv = (
            self.actions,
            self.log_probs,
            self.rewards,
            self.states,
            self.next_states,
            self.values,
            self.advantages,
        )

        # Shuffle our data.
        acts = acts[indices]
        probs = probs[indices]
        obs = obs[indices]
        vals = vals[indices]
        adv = adv[indices]

        batches = []
        n = len(acts) // batch_size

        # Split the shuffled data into batches.
        for i in range(n):
            start = i * batch_size
            stop = start + batch_size
            batches.append(
                [acts[start:stop],
                probs[start:stop],
                obs[start:stop],
                vals[start:stop],
                adv[start:stop]]
            )

        # Return the list of shuffled batches.
        return batches

    def _clamp_size(self):
        """
        Function to clamp the size of the experience buffer.
        :return: None.
        """

        # Check the difference between the length of our experience buffer and the maximum allowed length.
        start = len(self.actions) - self.max_size
        if start > 0:

            # Remove the oldest entries until our buffer reaches its maximum allowed size (FIFO).
            self.actions = self.actions[start:]
            self.log_probs = self.log_probs[start:]
            self.rewards = self.rewards[start:]
            self.states = self.states[start:]
            self.next_states = self.next_states[start:]
            self.dones = self.dones[start:]
            self.values = self.values[start:]
            self.advantages = self.advantages[start:]

    def clear(self):
        """
        Function to clear the experience buffer.
        :return: None.
        """
        del self.states
        del self.actions
        del self.log_probs
        del self.rewards
        del self.next_states
        del self.dones
        del self.truncated
        del self.values
        del self.advantages
        self.__init__(self.max_size, self.seed, self.device)