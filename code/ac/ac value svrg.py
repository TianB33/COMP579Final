import argparse
import gym
import numpy as np
from itertools import count
from collections import namedtuple, deque
from IPython.display import clear_output
import matplotlib.pyplot as plt
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.distributions import Categorical
from torchviz import make_dot
import time
import random
import os
import wandb

# Cart Pole
gamma = 0.95

env = gym.make('CartPole-v1')
eps = np.finfo(np.float32).eps.item()

# SavedAction works as a replay buffer
SavedAction = namedtuple('SavedAction', ['s', 'v', 'a', 'log_p'])

action_dim = env.action_space.n
state_dim  = env.observation_space.shape[0]

# these contains steps/rewards for many episodes
steps = []
rewards = []

# these contains steps/rewards for only the latest episode
latest_steps = []
latest_rewards = []

random.seed(33)
np.random.seed(33)
os.environ['PYTHONHASHSEED'] = str(33)
torch.manual_seed(33)
torch.cuda.manual_seed(33)
torch.backends.cudnn.deterministic = True

# 2 simple 2-layer NNs for the actor and critic
class Actor(nn.Module):
    def __init__(self, hidden_dim=16):
        super().__init__()

        self.hidden = nn.Linear(state_dim, hidden_dim)
        self.output = nn.Linear(hidden_dim, action_dim)

    def forward(self, s):
        outs = self.hidden(s)
        outs = F.relu(outs)
        probs = F.softmax(self.output(outs))
        return probs

class Value(nn.Module):
    def __init__(self, hidden_dim=16):
        super().__init__()

        self.hidden = nn.Linear(state_dim, hidden_dim)
        self.output = nn.Linear(hidden_dim, 1)

    def forward(self, s):
        outs = self.hidden(s)
        outs = F.relu(outs)
        value = self.output(outs)
        return value

actor = Actor()
value = Value()

# optimizer?
actor_optimizer = optim.SGD(actor.parameters(), lr=1e-4)

value_past_grad = [] # previous gradients of value model

def select_action(state):
    state = torch.from_numpy(state).float()
    prob, val = actor(state), value(state)

    m = Categorical(prob)
    action = m.sample()
    latest_steps.append(SavedAction(state, val, action, m.log_prob(action)))

    return action.item()

def finish_episode():
    '''
    The procedure after an episode. We record the trajectories, 
    accumulate the mean gradients w.r.t the snapshot model, without updating the parameters.
    '''
    R = 0
    policy_losses = []
    value_snapshot_losses = []
    returns = deque()

    global latest_rewards, latest_steps

    # calculate the true value using rewards returned from the environment
    for r in latest_rewards[::-1]:
        # calculate the discounted value
        R = r + gamma * R
        returns.appendleft(R)
    returns = torch.tensor(returns)

    for (s, v, a, log_p), R in zip(latest_steps, returns):
        advantage = R - v.item()
        policy_losses.append(-log_p * advantage)
        # since we don't update now, value is w.r.t the snapshot model
        value_snapshot_losses.append(F.smooth_l1_loss(v, torch.tensor([R])))
    
    value.zero_grad()
    actor.zero_grad()

    actor_loss = torch.stack(policy_losses).sum()
    value_snapshot_loss = torch.stack(value_snapshot_losses).sum()

    actor_loss.backward()
    value_snapshot_loss.backward()

    # remember the past gradients
    value_grad = [param.grad.clone() for param in value.parameters()]
    value_past_grad.append(value_grad)

    # update directly for the policy
    actor_optimizer.step()

    # remember the trajectories in a group
    rewards.append(latest_rewards)
    steps.append(latest_steps)

    # clear the buffer for this episode
    latest_rewards = []
    latest_steps = []


def finish_step(update_time, lr=1e-4):
    '''
    The procedure after a step.
    Now we can sample past episodes and do the corresponding updates.
    '''
    global value_past_grad, steps, rewards

    n = len(value_past_grad)

    # we first calculate mu.
    value_mu = [torch.zeros_like(param.grad) for param in value.parameters()]
    for p in value_past_grad:
        for i, g in enumerate(p):
            value_mu[i] += g / n

    for i_update in range(update_time):
        # pick a random previous episode t
        t = np.random.randint(0, n)
        value.zero_grad()

        # calculate the current gradient
        R = 0
        value_losses = []
        returns = deque()
        for r in rewards[t][::-1]:
            R = r + gamma * R
            returns.appendleft(R)
        returns = torch.tensor(returns)

        for (s, v, a, log_p), R in zip(steps[t], returns):
            cur_val = value(s)
            value_losses.append(F.smooth_l1_loss(cur_val, torch.tensor([R])))

        value_loss = torch.stack(value_losses).sum()
        value_loss.backward()

        with torch.no_grad():
            for i, p in enumerate(value.parameters()):
                new_p = p - lr * (value_mu[i] - value_past_grad[t][i] + p.grad)
                p.copy_(new_p)

    value_past_grad = []
    steps = []
    rewards = []


def main():
    ep_rewards = []
    groupsize = 10

    # we first freeze the model to get a 'full batch' of gradients
    # Then we use SVRG to update the model param multiple times
    for i_step in range(300):
        for j_episode in range(groupsize):
            # could change it to a decreasing number?
            state, _ = env.reset()
            ep_reward = 0

            while True:
                action = select_action(state)

                state, reward, term, trunc, _ = env.step(action)
                done = term or trunc
                latest_rewards.append(reward)
                ep_reward += reward
                if done:
                    break
            ep_rewards.append(ep_reward)
            # print(ep_reward)
            # time.sleep(3)
            finish_episode()
        finish_step(groupsize * 2)

        if i_step % 5 == 0:
            print('Step {}\tLast reward: {:.2f}'.format(i_step, ep_reward))
    clear_output(True)
    plt.figure(figsize=(20,5))
    plt.plot(ep_rewards)
    plt.savefig('ac q svrg cartpole.png')
    
if __name__ == '__main__':
    main()