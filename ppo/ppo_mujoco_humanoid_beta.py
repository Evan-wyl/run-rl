import argparse
import random
import time

import gym
from gym.utils.save_video import save_video

import numpy as np

import torch
from torch import functional as F
from torch import nn
from torch.utils.tensorboard import SummaryWriter
from torch import optim
from torch.distributions.normal import Normal
from torch.distributions.beta import Beta

import os
from distutils.util import strtobool

import logging
logging.basicConfig(filemode="w", format="%(asctime)s-%(name)s-%(levelname)s-%(message)s", level=logging.INFO)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exp-name", type=str, default=os.path.basename(__file__).rstrip(".py"),
                        help='the name of this experiment')
    parser.add_argument("--gym-id", type=str, default='Humanoid-v4',
                        help="the id of the gym environment")
    parser.add_argument('--model-file-name', type=str, default='humanoid_beta.pkl')
    parser.add_argument("--learning_rate", type=float, default=3e-4,
                        help='the learning rate of optimizer')
    parser.add_argument("--seed", type=int, default=2023,
                        help="seed of the experiment")
    parser.add_argument("--total-timesteps", type=int, default=10000000,
                        help='total timesteps of the experiments')
    parser.add_argument("--torch-deterministic", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
                        help="if toggled, `torch.backends.cudnn.deterministic=False`")
    parser.add_argument("--cuda", type=lambda x : bool(strtobool(x)), default=True, nargs="?", const=True,
                        help="if toggled, cuda will be enabled by default")
    parser.add_argument("--track", type=lambda x : bool(strtobool(x)), default=True, nargs="?", const=True,
                        help="if toggled, this experiment will be tracked with Weights and Biases")
    parser.add_argument("--wandb-project-name", type=str, default="ppo-humanoid-beta",
                        help="the wandb's project name")
    parser.add_argument("--wandb-entity", type=str, default=None,
                        help="the entity (team) of wandb's project")
    parser.add_argument("--capture-video", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
                        help="whether to capture videos of the agent performances (check out `videos` folder)")

    parser.add_argument("--num-envs", type=int, default=2,
                        help="the number of parallel game environments")
    parser.add_argument( "--num-steps", type=int, default=2048,
                         help="the number of steps to run in each environment per policy rollout")
    parser.add_argument("--anneal-lr", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
                        help="Toggle learning rate annealing for policy and value networks")
    parser.add_argument("--gae", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
                        help="Use GAE for advantage computation")
    parser.add_argument("--gamma", type=float, default=0.99,
                        help="the discount factor gamma")
    parser.add_argument("--gae-lambda", type=float, default=0.95,
                        help="the lambda for the general advantage estimation")
    parser.add_argument("--num-minibatches", type=int, default=32,
                        help="the number of mini-batches")
    parser.add_argument("--update-epochs", type=int, default=10,
                        help="the K epochs to update the policy")
    parser.add_argument("--norm-adv", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
                        help="Toggles advantages normalization")
    parser.add_argument("--clip-coef", type=float, default=0.2,
                        help="the surrogate clipping coefficient")
    parser.add_argument("--clip-vloss", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
                        help="Toggles whether or not to use a clipped loss for the value function, as per the paper.")
    parser.add_argument("--ent-coef", type=float, default=0.1,
                        help="coefficient of the entropy")
    parser.add_argument("--vf-coef", type=float, default=0.5,
                        help="coefficient of the value function")
    parser.add_argument("--max-grad-norm", type=float, default=0.3,
                        help="the maximum norm for the gradient clipping")
    parser.add_argument("--target-kl", type=float, default=None,
                        help="the target KL divergence threshold")

    args = parser.parse_args()
    args.batch_size = int(args.num_envs * args.num_steps)
    args.minibatch_size = int(args.batch_size // args.num_minibatches)
    return args


def make_env(gym_id, seed, idx, capture_video, run_name):
    def thunk():
        env = gym.make(gym_id)
        env = gym.wrappers.RecordEpisodeStatistics(env)
        if capture_video:
            if idx == 0:
                env = gym.wrappers.RecordVideo(env, f"videos/{run_name}")
        env = gym.wrappers.ClipAction(env)
        env = gym.wrappers.NormalizeObservation(env)
        env = gym.wrappers.TransformObservation(env, lambda obs : np.clip(obs, -10, 10))
        env = gym.wrappers.NormalizeReward(env)
        env = gym.wrappers.TransformReward(env, lambda reward: np.clip(reward, -10, 10))
        # env.seed(seed)
        env.action_space.seed(seed)
        env.observation_space.seed(seed)
        return env
    return thunk


def layer_init(layer, std=np.sqrt(2), bias_const=0.0, type='orthogonal'):
    if type == 'orthogonal':
        torch.nn.init.orthogonal_(layer.weight, std)
    else:
        torch.nn.init.xavier_uniform_(layer.weight)
    torch.nn.init.constant_(layer.bias, bias_const)
    return layer


class Agent(nn.Module):
    def __init__(self, envs):
        super(Agent, self).__init__()

        self.critic = nn.Sequential(
            layer_init(nn.Linear(np.array(envs.single_observation_space.shape).prod(), 64)),
            nn.Tanh(),
            layer_init(nn.Linear(64, 64)),
            nn.Tanh(),
            layer_init(nn.Linear(64, 1), std=1.0)
        )

        self.actor_alpha_pre_softplus = nn.Sequential(
            layer_init(nn.Linear(np.array(envs.single_observation_space.shape).prod(), 64), type='xavier_uniform'),
            nn.Tanh(),
            layer_init(nn.Linear(64, 64), type='xavier_uniform'),
            nn.Tanh(),
            layer_init(nn.Linear(64, np.prod(envs.single_action_space.shape)), std=0.01, type='xavier_uniform')
        )
        self.actor_beta_pre_softplus = nn.Sequential(
            layer_init(nn.Linear(np.array(envs.single_observation_space.shape).prod(), 64), type='xavier_uniform'),
            nn.Tanh(),
            layer_init(nn.Linear(64, 64), type='xavier_uniform'),
            nn.Tanh(),
            layer_init(nn.Linear(64, np.prod(envs.single_action_space.shape)), std=0.01, type='xavier_uniform')
        )

        self.action_space_high = torch.tensor(envs.single_action_space.high).to(device)
        self.action_space_low = torch.tensor(envs.single_action_space.low).to(device)

        self.softplus = torch.nn.Softplus()

    def get_value(self, x):
        return self.critic(x)

    def scale_by_action_bounds(self, beta_dist_samples):
        return beta_dist_samples * (self.action_space_high - self.action_space_low)  + self.action_space_low

    def inv_scale_by_action_bounds(self, actions):
        return (actions - self.action_space_low) / (self.action_space_high - self.action_space_low)

    def get_action_and_value(self, x, action=None):
        alpha = torch.add(self.softplus(self.actor_alpha_pre_softplus(x)), 1)
        beta = torch.add(self.softplus(self.actor_beta_pre_softplus(x)), 1)
        if torch.isnan(alpha).any() or torch.isnan(beta).any() or torch.isinf(alpha).any() or torch.isinf(beta).any():
            logging.info("alpha model parameters:")
            for k, v in enumerate(self.actor_alpha_pre_softplus.parameters()):
                logging.info("parameter_{}:{}".format(k, v))
            logging.info("-----------------------------------")
            logging.info("beta model parameters:")
            for k, v in enumerate(self.actor_beta_pre_softplus.parameters()):
                logging.info("parameter_{}:{}".format(k, v))
        probs = Beta(alpha, beta)
        if action is None:
            action = probs.sample()
            action = self.scale_by_action_bounds(action)
        return action, probs.log_prob(self.inv_scale_by_action_bounds(action)).sum(1), probs.entropy().sum(1), self.critic(x)


def test(model):
    env = make_env(args.gym_id, args.seed, 0, capture_video=True, run_name=run_name)()
    obs, infos = env.reset()
    done = False
    total_reward = 0
    obs = torch.tensor(obs).to(device)
    while not done:
        action, logprob, _, value = model.get_action_and_value(obs)
        next_obs, reward, done, _, infos = env.step(action.cpu().numpy())
        total_reward += reward
        obs = torch.tensor(next_obs).to(device)
    save_video(env.render('human'), f"videos/{run_name}/")
    env.close()

    return total_reward


if __name__ == '__main__':
    args = parse_args()
    run_name = f"{args.gym_id}_{args.exp_name}_{args.seed}_{int(time.time())}"
    logging.info("run_name: {}".format(run_name))

    model_param_path = "../models/"
    if not os.path.exists(model_param_path):
        os.makedirs(model_param_path)
    model_param_file = os.path.join(model_param_path, args.model_file_name)

    if args.track:
        import wandb
        wandb.login(key="bc7ee0a6fdbed43674ecaedba4653d0838149516")
        logging.info("log in wandb")

        wandb.init(
            project = args.wandb_project_name,
            entity = args.wandb_entity,
            sync_tensorboard = True,
            config = vars(args),
            name = run_name,
            monitor_gym = True,
            save_code = True
        )
        logging.info("wandb initialization")
        writer = SummaryWriter(f"runs/{run_name}")
        writer.add_text("hyperparameters",
        "|param|value|\n|-|-|\n%s" % ("\n".join([f"|{key}|{value}|" for key, value in vars(args).items()])),)

        random.seed(args.seed)
        np.random.seed(args.seed)
        torch.manual_seed(args.seed)
        torch.backends.cudnn.deterministic = args.torch_deterministic

        device = torch.device("cuda" if torch.cuda.is_available() and args.cuda else "cpu")

        envs = gym.vector.SyncVectorEnv(
            [make_env(args.gym_id, args.seed + i, i, args.capture_video, run_name) for i in range(args.num_envs)]
        )
        logging.info("Vectorize environment")

        assert isinstance(envs.single_action_space, gym.spaces.Box), "only continuous action space is supported"

        logging.info("construct agent......")
        agent = Agent(envs).to(device)
        optimizer = optim.Adam(agent.parameters(), lr=args.learning_rate, eps=1e-5)

        obs = torch.zeros((args.num_steps, args.num_envs) + envs.single_observation_space.shape).to(device)
        actions = torch.zeros((args.num_steps,args.num_envs) + envs.single_action_space.shape).to(device)
        logprobs = torch.zeros((args.num_steps, args.num_envs)).to(device)
        rewards = torch.zeros((args.num_steps, args.num_envs)).to(device)
        dones = torch.zeros((args.num_steps, args.num_envs)).to(device)
        values = torch.zeros((args.num_steps, args.num_envs)).to(device)

        global_step = 0
        start_time = time.time()
        next_obs = torch.Tensor(envs.reset()[0]).to(device)
        next_done = torch.zeros(args.num_envs).to(device)
        num_updates = args.total_timesteps // args.batch_size

        for update in range(1, num_updates + 1):
            if args.anneal_lr:
                frac = 1.0 - (update - 1.0) / num_updates
                lrnow = frac * args.learning_rate
                optimizer.param_groups[0]['lr'] = lrnow

            logging.info("agent rollout environment......")
            for step in range(0, args.num_steps):
                global_step += 1 * args.num_envs
                obs[step] = next_obs
                dones[step] = next_done

                with torch.no_grad():
                    action, logprob, _, value = agent.get_action_and_value(next_obs)
                    values[step] = value.flatten()
                actions[step] = action
                logprobs[step] = logprob

                next_obs, reward, done, _, infos = envs.step(action.cpu().numpy())
                rewards[step] = torch.tensor(reward).to(device).view(-1)

                next_obs, next_done = torch.Tensor(next_obs).to(device), torch.Tensor(done).to(device)

                if len(infos) > 0 and isinstance(infos, dict) and "final_info" in infos.keys():
                    for item in infos['final_info']:
                        if isinstance(item, dict) and 'episode' in item.keys():
                            logging.info(item)
                            logging.info(f"global_step={global_step}, episodic_return={item['episode']['r']}")
                            writer.add_scalar("charts/episodic_return", item["episode"]["r"], global_step)
                            writer.add_scalar("charts/episodic_length", item["episode"]["l"], global_step)
                            break

            with torch.no_grad():
                next_value = agent.get_value(next_obs).reshape(1, -1)
                if args.gae:
                    logging.info("calculating advantage")
                    advantages = torch.zeros_like(rewards).to(device)
                    lastgaelam = 0
                    for t in reversed(range(args.num_steps)):
                        if t == args.num_steps - 1:
                            nextnonterminal = 1.0 - next_done
                            nextvalues = next_value
                        else:
                            nextnonterminal = 1.0 - dones[t + 1]
                            nextvalues = values[t + 1]
                        delta = rewards[t] + args.gamma * nextvalues * nextnonterminal - values[t]
                        lastgaelam = delta + args.gamma * args.gae_lambda * nextnonterminal * lastgaelam
                        advantages[t] = lastgaelam
                    returns = advantages + values
                else:
                    logging.info("calculating returns")
                    returns = torch.zeros_like(rewards).to(device)
                    for t in reversed(range(args.num_steps)):
                        if t == args.num_steps - 1:
                            nextnonterminal = 1 - next_done
                            next_return = next_value
                        else:
                            nextnonterminal = 1.0 - dones[t + 1]
                            next_return = returns[t + 1]
                        returns[t] = rewards[t] + args.gamma * nextnonterminal * next_return
                    advantages = returns - values

            b_obs = obs.reshape((-1, ) + envs.single_observation_space.shape)
            b_logprobs = logprobs.reshape(-1)
            b_actions = actions.reshape((-1, ) + envs.single_action_space.shape)
            b_advantages = advantages.reshape(-1)
            b_returns = returns.reshape(-1)
            b_values = values.reshape(-1)

            b_inds = np.arange(args.batch_size)
            clipfracs = []
            for epoch in range(args.update_epochs):
                np.random.shuffle(b_inds)
                for start in range(0, args.batch_size, args.minibatch_size):
                    end = start + args.minibatch_size
                    mb_inds = b_inds[start:end]

                    _, newlogprob, entropy, newvalue = agent.get_action_and_value(b_obs[mb_inds], b_actions[mb_inds])
                    logratio = newlogprob - b_logprobs[mb_inds]
                    ratio = logratio.exp()

                    with torch.no_grad():
                        old_approx_kl = (-logratio).mean()
                        approx_kl = ((ratio - 1) - logratio).mean()
                        clipfracs += [((ratio - 1.0).abs() > args.clip_coef).float().mean().item()]

                    mb_advantages = b_advantages[mb_inds]
                    if args.norm_adv:
                        mb_advantages = (mb_advantages - mb_advantages.mean()) / (mb_advantages.std() + 1e-8)

                    logging.info("calculating policy loss")
                    pg_loss1 = -mb_advantages * ratio
                    pg_loss2 = -mb_advantages * torch.clamp(ratio, 1 - args.clip_coef, 1 + args.clip_coef)
                    pg_loss = torch.max(pg_loss1, pg_loss2).mean()

                    logging.info("calculating value loss......")
                    newvalue = newvalue.view(-1)
                    if args.clip_vloss:
                        logging.info("value loss clipped......")
                        v_loss_unclipped = (newvalue - b_returns[mb_inds]) ** 2
                        v_clipped = b_values[mb_inds] + torch.clamp(newvalue - b_values[mb_inds], -args.clip_coef, args.clip_coef)
                        v_loss_clipped = (v_clipped - b_values[mb_inds]) ** 2
                        v_loss_max = torch.max(v_loss_unclipped, v_loss_unclipped)
                        v_loss = 0.5 * v_loss_max.mean()
                    else:
                        v_loss = 0.5 * ((newvalue - b_values[mb_inds]) ** 2).mean()

                    logging.info("calculating policy entropy")
                    entropy_loss = entropy.mean()
                    loss = pg_loss - args.ent_coef * entropy_loss + v_loss * args.vf_coef

                    logging.info("calculating gradient")
                    optimizer.zero_grad()
                    loss.backward()
                    nn.utils.clip_grad_norm_(agent.parameters(), args.max_grad_norm)
                    optimizer.step()

                if args.target_kl is not None:
                    if approx_kl > args.target_kl:
                        break

                y_pred, y_true = b_values.cpu().numpy(), b_returns.cpu().numpy()
                var_y = np.var(y_true)
                explained_var = np.nan if var_y == 0 else 1 - np.var(y_true - y_pred) / var_y

            writer.add_scalar("charts/learning_rate", optimizer.param_groups[0]['lr'], global_step)
            writer.add_scalar("losses/value_loss", v_loss.item(), global_step)
            writer.add_scalar("losses/policy_loss", pg_loss.item(), global_step)
            writer.add_scalar("losses/entropy", entropy_loss.item(), global_step)
            writer.add_scalar("losses/old_approx_kl", old_approx_kl.item(), global_step)
            writer.add_scalar("losses/approx_kl", approx_kl.item(), global_step)
            writer.add_scalar("losses/clipfrac", np.mean(clipfracs), global_step)
            writer.add_scalar("losses/explained_variance", explained_var, global_step)
            print("SPS:", int(global_step / (time.time() - start_time)))
            writer.add_scalar("charts/SPS", int(global_step / (time.time() - start_time)), global_step)

        logging.info('time spending {} second.'.format((time.time() - start_time)))

        logging.info('save models......')
        torch.save(agent.state_dict(), model_param_file)
        logging.info('model testing......')
        total_reward = 0
        for i in range(10):
            reward = test(agent)
            total_reward += reward
        logging.info('total rewards:{}'.format(total_reward))

        envs.close()
        writer.close()












