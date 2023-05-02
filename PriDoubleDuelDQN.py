import os
import sys
import random

sys.path.append("game/")
import wrapped_flappy_bird as game
import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import time
from collections import namedtuple
import matplotlib.pyplot as plt

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
learning_rate = 1e-4
SYNC_TARGET_FRAMES = 30
GAMMA = 0.99
EPSILON_START = 0.1
EPSILON_FINAL = 0.001
EPSILON_DECAY_FRAMES = (10 ** 4) / 3
num_iterations = 1000000

class DuelQNetwork(nn.Module):
    def __init__(self):
        super(DuelQNetwork, self).__init__()
        # Discount factor
        self.gamma = 0.99
        # Epsilon values for ϵ greedy exploration
        self.initial_epsilon = 1
        self.final_epsilon = 0.001
        self.EPSILON_DECAY_FRAMES = (10 ** 4) / 3
        self.replay_memory_size = 2000
        self.num_iterations = num_iterations
        self.minibatch_size = 128
        self.episode_durations = []
        # Use gpu if it is availiable
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        # Use same network architecture as DeepMind
        # Input is 4 frames stacked to infer velocity
        self.conv1 = nn.Conv2d(4, 32, 8, 4)
        self.conv2 = nn.Conv2d(32, 64, 4, 2)
        self.conv3 = nn.Conv2d(64, 64, 3, 1)
        self.fc1 = nn.Linear(3136, 512)
        # Output 2 values: fly up and do nothing
        self.fc2 = nn.Linear(512, 2)
        self.relu = nn.ReLU(inplace=True)
        self.fca = nn.Sequential(
            nn.Linear(3136, 512),
            nn.ReLU(),
            nn.Linear( 512, 2 )
        )
        self.fcv = nn.Sequential(
            nn.Linear(3136,512),
            nn.ReLU(),
            nn.Linear(512,1)
        )
        print(self.device)

    def forward(self, x):
        x = self.relu(self.conv1(x))
        x = self.relu(self.conv2(x))
        x = self.relu(self.conv3(x))
        # Flatten output to feed into fully connected layers
        x = x.view(x.size()[0], -1)
        #x = self.relu(self.fc1(x))
        #x = self.fc2(x)
        act = self.fca(x)
        val = self.fcv(x).expand(x.size(0),2)
        x = val + act - act.mean(1).unsqueeze(1).expand(x.size(0), 2)
        return x

# Transition that maps (state, action) pairs to their (next_state, reward) result
Transition = namedtuple('Transition', ('state', 'action', 'next_state', 'reward', 'terminal','lossnp'))


class ReplayMemory:
    """A cyclic buffer of bounded size that holds the transitions observed recently"""

    def __init__(self, capacity):
        self.capacity = capacity
        self.memory = []
        self.iter = 0

    def push(self, *args):
        """Saves a transition."""
        if len(self.memory) < self.capacity:
            self.memory.append(None)
        self.memory[self.iter% self.capacity] = Transition(*args)
        self.iter = self.iter+1

    def sample(self, batch_size):
        """Selects a random batch of transitions for training."""
        if self.iter>10:
            lossnp = [m[5] for m in self.memory]
            probs = np.array(lossnp) / sum(lossnp)
            samplelist = [int(i) for i in np.random.choice(np.arange(min(self.iter,self.capacity)), batch_size, p=probs)]
            return [self.memory[i] for i in samplelist]
        elif self.iter<11:
            return random.sample(self.memory, batch_size)

    def __len__(self):
        return len(self.memory)

def init_weights(m):
    if type(m) == nn.Conv2d or type(m) == nn.Linear:
        torch.nn.init.uniform_(m.weight, -0.01, 0.01)
        m.bias.data.fill_(0.01)



def resize_and_bgr2gray_to_tensor(image):
    # Crop out the floor
    image = image[55:288, 0:404]
    # Convert to grayscale and resize image
    image_data = cv2.cvtColor(cv2.resize(image, (84, 84)), cv2.COLOR_BGR2GRAY)
    image_data[image_data > 0] = 255
    image_data = np.reshape(image_data, (84, 84, 1))
    image_tensor = image_data.transpose(2, 0, 1)
    image_tensor = image_tensor.astype(np.float32)
    image_tensor = torch.from_numpy(image_tensor)
    if torch.cuda.is_available():
        image_tensor = image_tensor.cuda()
    return image_tensor
total_rewards = []




def train(net,tarnet, start):
    """
    Trains the Deep Q-Network
    Args:
        net: torch.nn model
        start: time start training
    """
    # Initialize optimizer
    all_losses = []
    optimizer = optim.Adam(net.parameters(), lr=learning_rate)
    # Initialize loss function
    loss_func = nn.MSELoss()
    # Initialize game
    game_state = game.GameState()
    # Initialize replay memory
    memory = ReplayMemory(net.replay_memory_size)
    # Initial action is do nothing
    action = torch.zeros(2, dtype=torch.float32)
    action[0] = 1
    # [1, 0] is do nothing, [0, 1] is fly up
    image_data, reward, terminal = game_state.frame_step(action)
    # Image Preprocessing
    image_data = resize_and_bgr2gray_to_tensor(image_data)
    state = torch.cat((image_data, image_data, image_data, image_data)).unsqueeze(0)
    # Initialize epsilon value
    epsilon = net.initial_epsilon
    # Epsilon annealing
    # epsilon_decrements = np.linspace(net.initial_epsilon, net.final_epsilon, net.num_iterations)


    t = 0

    lossnp=0.00000001

    # Get output from the neural network
    output = net(state)[0]
    # Initialize action
    action = torch.zeros(2, dtype=torch.float32)
    if torch.cuda.is_available():
        action = action.cuda()
    # Epsilon greedy exploration
    random_action = random.random() <= epsilon
    # if random_action:
    #     print("Performed random action!")
    action_index = [torch.randint(2, torch.Size([]), dtype=torch.int)
                    if random_action
                    else torch.argmax(output)][0]
    if torch.cuda.is_available():
        action_index = action_index.cuda()
    action[action_index] = 1
    action = action.unsqueeze(0)
    reward = torch.from_numpy(np.array([reward], dtype=np.float32)).unsqueeze(0)
    memory.push(state, action, reward, state, terminal, lossnp)

    epsi_dec = 0
    total_rewards = []

    # Train Loop
    print("Start Episode", 0)
    for iteration in range(net.num_iterations):
        # Get output from the neural network
        output = net(state)[0]
        # Initialize action
        action = torch.zeros(2, dtype=torch.float32)
        if torch.cuda.is_available():
            action = action.cuda()
        # Epsilon greedy exploration
        random_action = random.random() <= epsilon
        # if random_action:
        #     print("Performed random action!")
        action_index = [torch.randint(2, torch.Size([]), dtype=torch.int)
                        if random_action
                        else torch.argmax(output)][0]
        if torch.cuda.is_available():
            action_index = action_index.cuda()
        action[action_index] = 1

        # Get next state and reward
        image_data_1, reward, terminal = game_state.frame_step(action)
        image_data_1 = resize_and_bgr2gray_to_tensor(image_data_1)
        state_1 = torch.cat((state.squeeze(0)[1:, :, :], image_data_1)).unsqueeze(0)
        action = action.unsqueeze(0)
        reward = torch.from_numpy(np.array([reward], dtype=np.float32)).unsqueeze(0)


        # Sample random minibatch
        minibatch = memory.sample(min(len(memory), net.minibatch_size))

        # Unpack minibatch
        state_batch = torch.cat(tuple(d[0] for d in minibatch))
        action_batch = torch.cat(tuple(d[1] for d in minibatch))
        reward_batch = torch.cat(tuple(d[2] for d in minibatch))
        state_1_batch = torch.cat(tuple(d[3] for d in minibatch))
        # terminal_batch = torch.cat(tuple(d[4] for d in minibatch))

        if torch.cuda.is_available():
            state_batch = state_batch.cuda()
            action_batch = action_batch.cuda()
            reward_batch = reward_batch.cuda()
            state_1_batch = state_1_batch.cuda()

        # Get output for the next state
        output_1_batch = tarnet(state_1_batch)
        # Set y_j to r_j for terminal state, otherwise to r_j + gamma*max(Q)
        expected_value = torch.cat(tuple(reward_batch[i] if minibatch[i][4]
                                  else reward_batch[i] + net.gamma * torch.max(output_1_batch[i])
                                  for i in range(len(minibatch))))
        # Extract Q-value (this part i don't understand)
        evalq = torch.sum(net(state_batch) * action_batch, dim=1)
        optimizer.zero_grad()
        # Returns a new Tensor, detached from the current graph, the result will never require gradient
        expected_value = expected_value.detach()
        # Calculate loss
        loss = loss_func(evalq, expected_value)
        lossnp =  float(loss)

        memory.push(state, action, reward, state_1, terminal, lossnp)
        # Epsilon annealing
        epsilon = max(EPSILON_FINAL, EPSILON_START - epsi_dec / EPSILON_DECAY_FRAMES)
        if reward != 0.01:
            total_rewards.append(reward)
            epsi_dec += 1


        # Do backward pass
        loss.backward()
        optimizer.step()

        # Set state to be state_1
        state = state_1
        # update parameters in targetnet

        if iteration % SYNC_TARGET_FRAMES == 0:
            tarnet.load_state_dict(net.state_dict())

        if iteration % 25000 == 0:
            torch.save(net, "PriDoubleDuelDQNmodel_weights/current_model_" + str(iteration) + ".pth")

        if iteration % 500 == 0:
            print("iteration:", iteration, "elapsed time:", time.time() - start, "epsilon:", epsilon, "Q max:",
                  np.max(output.cpu().detach().numpy()))
        t += 1
        # Plot duration
        if terminal:
            net.episode_durations.append(t)
            meant = np.mean(net.episode_durations[-100:])
            print("Game:", len(net.episode_durations), "Duration:", t, "Mean_Duration:" ,meant)
            if (iteration>500000) & (t>800) & (meant>400):
                print("finaliteration:", iteration, "finalelapsed time:", time.time() - start)
                np.save('finalresults/PriDoubleDuelDQNfinal.npy',[iteration,time.time() - start])
                save_durations(net.episode_durations)
                torch.save(net, "PriDoubleDuelDQNmodel_weights/final_model.pth")
                break
            # plot_durations(net.episode_durations)
            # save_durations(net.episode_durations)
            t = 0


def save_durations(episode_durations):
    durations_t = torch.tensor(episode_durations, dtype=torch.float)
    np.save("duration/PriDoubleDuelDQNduration.npy", durations_t.numpy())


def plot_durations(episode_durations):
    """Plot durations of episodes and average over last 100 episodes"""
    plt.figure(1)
    plt.clf()
    durations_t = torch.tensor(episode_durations, dtype=torch.float)
    plt.title('Training...')
    plt.xlabel('Episode')
    plt.ylabel('Duration')
    plt.scatter(np.arange(1, len(durations_t) + 1), durations_t.numpy(), color='green', alpha=0.5)

    # Take 100 episode averages and plot them too
    if len(durations_t) >= 101:
        means = durations_t.unfold(0, 100, 1).mean(1).view(-1)
        # means = torch.cat((torch.zeros(99), means))
        # plt.plot(means.numpy(),color = 'orange',lw=4)
        plt.plot(np.arange(1, len(durations_t) - 98) + 99, means.numpy(), color='orange', lw=4)

    plt.pause(0.001)

def test(net):
    game_state = game.GameState()

    action = torch.zeros(2, dtype=torch.float32)
    action[0] = 1
    image_data, reward, terminal = game_state.frame_step(action)
    image_data = resize_and_bgr2gray_to_tensor(image_data)
    state = torch.cat((image_data, image_data, image_data, image_data)).unsqueeze(0)

    while True:
        # Get output from the neural network
        output = model(state)[0]

        action = torch.zeros(2, dtype=torch.float32)
        if torch.cuda.is_available():
            action = action.cuda()

        # Get action
        action_index = torch.argmax(output)
        if torch.cuda.is_available():
            action_index = action_index.cuda()
        action[action_index] = 1

        # Get next state
        image_data_1, reward, terminal = game_state.frame_step(action)
        image_data_1 = resize_and_bgr2gray_to_tensor(image_data_1)
        state_1 = torch.cat((state.squeeze(0)[1:, :, :], image_data_1)).unsqueeze(0)

        state = state_1


if __name__ == "__main__":

    mode = sys.argv[1]
    # mode = 'train'

    plt.ion()
    if mode == 'test':
        model = torch.load(
            #'pretrained_model/current_model_6250000.pth',
            'PriDoubleDuelDQNmodel_weights/current_model_6250000.pth',
            map_location='cpu' if not torch.cuda.is_available() else None
        ).eval()
        if torch.cuda.is_available():  # put on GPU if CUDA is available
            model = model.cuda()
        test(model)
    elif mode == "train":
        if not os.path.exists('PriDoubleDuelDQNmodel_weights/'):
            os.mkdir('PriDoubleDuelDQNmodel_weights/')

        Q = DuelQNetwork()
        Q.to(Q.device)
        Q.apply(init_weights)
        tarQ = DuelQNetwork()
        tarQ.to(tarQ.device)
        tarQ.apply(init_weights)
        start = time.time()
        train(Q,tarQ,start)

        plt.ioff()
        plt.show()