# Reference: https://arxiv.org/pdf/1509.02971.pdf
# https://github.com/seungeunrho/minimalRL/blob/master/ddpg.py
# https://github.com/openai/spinningup/blob/master/spinup/algos/pytorch/ddpg/ddpg.py

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.utils.tensorboard import SummaryWriter

import argparse
from distutils.util import strtobool
import collections
import numpy as np
import gym
import pybullet_envs
from gym.wrappers import TimeLimit, Monitor
from gym.spaces import Discrete, Box, MultiBinary, MultiDiscrete, Space
import time
import random
import os

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='DDPG agent')
    # Common arguments
    parser.add_argument('--exp-name', type=str, default=os.path.basename(__file__).rstrip(".py"),
                        help='the name of this experiment')
    parser.add_argument('--gym-id', type=str, default="HopperBulletEnv-v0",
                        help='the id of the gym environment')
    parser.add_argument('--learning-rate', type=float, default=3e-4,
                        help='the learning rate of the optimizer')
    parser.add_argument('--seed', type=int, default=1,
                        help='seed of the experiment')
    parser.add_argument('--episode-length', type=int, default=0,
                        help='the maximum length of each episode')
    parser.add_argument('--total-timesteps', type=int, default=1000000,
                        help='total timesteps of the experiments')
    parser.add_argument('--torch-deterministic', type=lambda x:bool(strtobool(x)), default=True, nargs='?', const=True,
                        help='if toggled, `torch.backends.cudnn.deterministic=False`')
    parser.add_argument('--cuda', type=lambda x:bool(strtobool(x)), default=True, nargs='?', const=True,
                        help='if toggled, cuda will not be enabled by default')
    parser.add_argument('--prod-mode', type=lambda x:bool(strtobool(x)), default=False, nargs='?', const=True,
                        help='run the script in production mode and use wandb to log outputs')
    parser.add_argument('--capture-video', type=lambda x:bool(strtobool(x)), default=False, nargs='?', const=True,
                        help='weather to capture videos of the agent performances (check out `videos` folder)')
    parser.add_argument('--wandb-project-name', type=str, default="cleanRL",
                        help="the wandb's project name")
    parser.add_argument('--wandb-entity', type=str, default=None,
                        help="the entity (team) of wandb's project")
    
    # Algorithm specific arguments
    parser.add_argument('--buffer-size', type=int, default=int(1e6),
                         help='the replay memory buffer size')
    parser.add_argument('--gamma', type=float, default=0.99,
                        help='the discount factor gamma')
    parser.add_argument('--tau', type=float, default=0.005,
                        help="target smoothing coefficient (default: 0.005)")
    parser.add_argument('--max-grad-norm', type=float, default=0.5,
                        help='the maximum norm for the gradient clipping')
    parser.add_argument('--batch-size', type=int, default=256,
                        help="the batch size of sample from the reply memory")
    parser.add_argument('--action-noise', default="normal", choices=["ou", 'normal'],
                         help='Selects the scheme to be used for weights initialization')
    parser.add_argument('--learning-starts', type=int, default=25e3,
                        help="timestep to start learning")
    parser.add_argument('--policy-frequency', type=int, default=2,
                        help="the frequency of training policy (delayed)")
    parser.add_argument('--noise-clip', type=float, default=0.5,
                         help='noise clip parameter of the Target Policy Smoothing Regularization')
    args = parser.parse_args()
    if not args.seed:
        args.seed = int(time.time())

# TRY NOT TO MODIFY: setup the environment
experiment_name = f"{args.gym_id}__{args.exp_name}__{args.seed}__{int(time.time())}"
writer = SummaryWriter(f"runs/{experiment_name}")
writer.add_text('hyperparameters', "|param|value|\n|-|-|\n%s" % (
        '\n'.join([f"|{key}|{value}|" for key, value in vars(args).items()])))
if args.prod_mode:
    import wandb
    wandb.init(project=args.wandb_project_name, entity=args.wandb_entity, sync_tensorboard=True, config=vars(args), name=experiment_name, monitor_gym=True, save_code=True)
    writer = SummaryWriter(f"/tmp/{experiment_name}")


# TRY NOT TO MODIFY: seeding
device = torch.device('cuda' if torch.cuda.is_available() and args.cuda else 'cpu')
env = gym.make(args.gym_id)
random.seed(args.seed)
np.random.seed(args.seed)
torch.manual_seed(args.seed)
torch.backends.cudnn.deterministic = args.torch_deterministic
env.seed(args.seed)
env.action_space.seed(args.seed)
env.observation_space.seed(args.seed)
# respect the default timelimit
assert isinstance(env.action_space, Box), "only continuous action space is supported"
assert isinstance(env, TimeLimit) or int(args.episode_length), "the gym env does not have a built in TimeLimit, please specify by using --episode-length"
if isinstance(env, TimeLimit):
    if int(args.episode_length):
        env._max_episode_steps = int(args.episode_length)
    args.episode_length = env._max_episode_steps
else:
    env = TimeLimit(env, int(args.episode_length))
if args.capture_video:
    env = Monitor(env, f'videos/{experiment_name}')

# taken from https://github.com/openai/baselines/blob/master/baselines/ddpg/noise.py
class NormalActionNoise():
    def __init__(self, mu, sigma=0.1):
        self.mu = mu
        self.sigma = sigma

    def __call__(self):
        return np.random.normal(self.mu, self.sigma)

    def reset(self):
        pass

    def __repr__(self):
        return 'NormalActionNoise(mu={}, sigma={})'.format(self.mu, self.sigma)


# Based on http://math.stackexchange.com/questions/1287634/implementing-ornstein-uhlenbeck-in-matlab
class OrnsteinUhlenbeckActionNoise():
    def __init__(self, mu, sigma=0.1, theta=.15, dt=1e-2, x0=None):
        self.theta = theta
        self.mu = mu
        self.sigma = sigma
        self.dt = dt
        self.x0 = x0
        self.reset()

    def __call__(self):
        x = self.x_prev + self.theta * (self.mu - self.x_prev) * self.dt + self.sigma * np.sqrt(self.dt) * np.random.normal(size=self.mu.shape)
        self.x_prev = x
        return x

    def reset(self):
        self.x_prev = self.x0 if self.x0 is not None else np.zeros_like(self.mu)

    def __repr__(self):
        return 'OrnsteinUhlenbeckActionNoise(mu={}, sigma={})'.format(self.mu, self.sigma)

# modified from https://github.com/seungeunrho/minimalRL/blob/master/dqn.py#
class ReplayBuffer():
    def __init__(self, buffer_limit):
        self.buffer = collections.deque(maxlen=buffer_limit)
    
    def put(self, transition):
        self.buffer.append(transition)
    
    def sample(self, n):
        mini_batch = random.sample(self.buffer, n)
        s_lst, a_lst, r_lst, s_prime_lst, done_mask_lst = [], [], [], [], []
        
        for transition in mini_batch:
            s, a, r, s_prime, done_mask = transition
            s_lst.append(s)
            a_lst.append(a)
            r_lst.append(r)
            s_prime_lst.append(s_prime)
            done_mask_lst.append(done_mask)

        return np.array(s_lst), np.array(a_lst), \
               np.array(r_lst), np.array(s_prime_lst), \
               np.array(done_mask_lst)

# ALGO LOGIC: initialize agent here:
class QNetwork(nn.Module):
    def __init__(self):
        super(QNetwork, self).__init__()
        self.fc1 = nn.Linear(
            np.array(env.observation_space.shape).prod()+np.prod(env.action_space.shape), 256)
        self.fc2 = nn.Linear(256, 256)
        self.fc3 = nn.Linear(256, 1)

    def forward(self, x, a):
        x = torch.Tensor(x).to(device)
        x = torch.cat([x, a], 1)
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x = self.fc3(x)
        return x

class Actor(nn.Module):
    def __init__(self):
        super(Actor, self).__init__()
        self.fc1 = nn.Linear(np.array(env.observation_space.shape).prod(), 256)
        self.fc2 = nn.Linear(256, 256)
        self.fc_mu = nn.Linear(256, np.prod(env.action_space.shape))

    def forward(self, x):
        x = torch.Tensor(x).to(device)
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        return torch.tanh(self.fc_mu(x))

def linear_schedule(start_sigma: float, end_sigma: float, duration: int, t: int):
    slope =  (end_sigma - start_sigma) / duration
    return max(slope * t + start_sigma, end_sigma)

rb = ReplayBuffer(args.buffer_size)
actor = Actor().to(device)
qf1 = QNetwork().to(device)
qf2 = QNetwork().to(device)
qf1_target = QNetwork().to(device)
qf2_target = QNetwork().to(device)
target_actor = Actor().to(device)
target_actor.load_state_dict(actor.state_dict())
qf1_target.load_state_dict(qf1.state_dict())
qf2_target.load_state_dict(qf2.state_dict())
q_optimizer = optim.Adam(list(qf1.parameters()) + list(qf2.parameters()), lr=args.learning_rate)
actor_optimizer = optim.Adam(list(actor.parameters()), lr=args.learning_rate)
loss_fn = nn.MSELoss()
exploration_noise = NormalActionNoise(np.zeros(np.prod(env.action_space.shape))) if args.action_noise == "normal" else OrnsteinUhlenbeckActionNoise(np.zeros(np.prod(env.action_space.shape)))
policy_noise = NormalActionNoise(np.zeros(np.prod(env.action_space.shape))) if args.action_noise == "normal" else OrnsteinUhlenbeckActionNoise(np.zeros(np.prod(env.action_space.shape)))

# TRY NOT TO MODIFY: start the game
obs = env.reset()
episode_reward = 0
exploration_noise.sigma = 0.1
policy_noise.sigma = 0.2

for global_step in range(args.total_timesteps):
    # ALGO LOGIC: put action logic here
    if global_step < args.learning_starts:
        action = env.action_space.sample()
    else:
        action = actor.forward(obs.reshape((1,)+obs.shape))
        action = (action.tolist()[0] + exploration_noise()).clip(env.action_space.low, env.action_space.high)

    # TRY NOT TO MODIFY: execute the game and log data.
    next_obs, reward, done, info = env.step(action)
    episode_reward += reward
    
    # ALGO LOGIC: training.
    rb.put((obs, action, reward, next_obs, done))
    if global_step > args.learning_starts:
        s_obs, s_actions, s_rewards, s_next_obses, s_dones = rb.sample(args.batch_size)
        with torch.no_grad():
            clipped_noise = np.clip(policy_noise(), -args.noise_clip, args.noise_clip)
            next_state_actions = (
                actor.forward(s_next_obses) + torch.Tensor(clipped_noise).to(device)
            ).clamp(env.action_space.low[0], env.action_space.high[0])
            qf1_next_target = qf1_target.forward(s_next_obses, next_state_actions)
            qf2_next_target = qf2_target.forward(s_next_obses, next_state_actions)
            min_qf_next_target = torch.min(qf1_next_target, qf2_next_target)
            next_q_value = torch.Tensor(s_rewards).to(device) + (1 - torch.Tensor(s_dones).to(device)) * args.gamma * (min_qf_next_target).view(-1)

        qf1_a_values = qf1.forward(s_obs, torch.Tensor(s_actions).to(device)).view(-1)
        qf2_a_values = qf2.forward(s_obs, torch.Tensor(s_actions).to(device)).view(-1)
        qf1_loss = loss_fn(qf1_a_values, next_q_value)
        qf2_loss = loss_fn(qf2_a_values, next_q_value)

        # optimize the midel
        q_optimizer.zero_grad()
        qf1_loss.backward()
        qf2_loss.backward()
        nn.utils.clip_grad_norm_(list(qf1.parameters())+list(qf2.parameters()), args.max_grad_norm)
        q_optimizer.step()

        if global_step % args.policy_frequency == 0:
            actor_loss = -qf1.forward(s_obs, actor.forward(s_obs)).mean()
            actor_optimizer.zero_grad()
            actor_loss.backward()
            nn.utils.clip_grad_norm_(list(actor.parameters()), args.max_grad_norm)
            actor_optimizer.step()

            # update the target network
            for param, target_param in zip(actor.parameters(), target_actor.parameters()):
                target_param.data.copy_(args.tau * param.data + (1 - args.tau) * target_param.data)
            for param, target_param in zip(qf1.parameters(), qf1_target.parameters()):
                target_param.data.copy_(args.tau * param.data + (1 - args.tau) * target_param.data)
            for param, target_param in zip(qf2.parameters(), qf2_target.parameters()):
                target_param.data.copy_(args.tau * param.data + (1 - args.tau) * target_param.data)

        if global_step % 100 == 0:
            writer.add_scalar("losses/qf1_loss", qf1_loss.item(), global_step)
            writer.add_scalar("losses/actor_loss", actor_loss.item(), global_step)

    # TRY NOT TO MODIFY: CRUCIAL step easy to overlook 
    obs = next_obs

    if done:
        exploration_noise.reset()
        # TRY NOT TO MODIFY: record rewards for plotting purposes
        print(f"global_step={global_step}, episode_reward={episode_reward}")
        writer.add_scalar("charts/episode_reward", episode_reward, global_step)
        obs, episode_reward = env.reset(), 0

env.close()
writer.close()
