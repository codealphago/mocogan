# coding: utf-8


import os
import glob
import time
import math
import skvideo.io
import numpy as np
import torch
from torch import nn, optim
from torch.autograd import Variable

from models import Discriminator_I, Discriminator_V, Generator_I, GRU

seed = 0
torch.manual_seed(seed)
np.random.seed(seed)
cuda = True
if cuda == True:
    torch.cuda.set_device(0)


''' prepare dataset '''

current_path = os.path.dirname(__file__)
resized_path = os.path.join(current_path, 'resized_data')
files = glob.glob(resized_path+'/*')
videos = [ skvideo.io.vread(file) for file in files ]
# transpose each video to (nc, n_frames, img_size, img_size), and devide by 255
videos = [ video.transpose(3, 0, 1, 2) / 255.0 for video in videos ]


''' prepare video sampling '''

n_videos = len(videos)
batch_size = 16
T = 16

# for true video
def trim(video):
    start = np.random.randint(0, video.shape[1] - (T+1))
    end = start + T
    return video[:, start:end, :, :]

# for input noises to generate fake video 
# note that noises are trimmed randomly from n_frames to T for efficiency
def trim_noise(noise):
    start = np.random.randint(0, noise.size(1) - (T+1))
    end = start + T
    return noise[:, start:end, :, :, :]

def random_choice():
    X = []
    for _ in range(batch_size):
        video = videos[np.random.randint(0, n_videos-1)]
        video = torch.Tensor(trim(video))
        X.append(video)
    X = torch.stack(X)
    return X

# video length distribution
video_lengths = [video.shape[1] for video in videos]


''' set models '''

img_size = 96
nc = 3
ndf = 64 # from dcgan
ngf = 64
d_E = 100 # guess
hidden_size = 100 # guess
d_C = 50
d_M = 10
nz  = d_C + d_M
criterion = nn.BCELoss()

dis_i = Discriminator_I(nc, ndf, ngpu=2)
dis_v = Discriminator_V(nc, ndf, T=T, ngpu=2)
gen_i = Generator_I(nc, ngf, nz, ngpu=2)
gru = GRU(d_E, hidden_size, d_M)


''' prepare for train '''

label = torch.FloatTensor()

def timeSince(since):
    now = time.time()
    s = now - since
    d = math.floor(s / ((60**2)*24))
    h = math.floor(s / (60**2)) - d*((60**2)*24)
    m = math.floor(s / 60) - h*(60**2) - d*((60**2)*24)
    s = s - m*60 - h*(60**2) - d*((60**2)*24)
    return '%dd %dh %dm %ds' % (d, h, m, s)


''' adjust to cuda '''

if cuda == True:
    dis_i.cuda()
    dis_v.cuda()
    gen_i.cuda()
    gru.cuda()
    criterion.cuda()
    label = label.cuda()


# setup optimizer
lr = 0.0002
betas=(0.5, 0.999)
optim_Di  = optim.Adam(dis_i.parameters(), lr=lr, betas=betas)
optim_Dv  = optim.Adam(dis_v.parameters(), lr=lr, betas=betas)
optim_Gi  = optim.Adam(gen_i.parameters(), lr=lr, betas=betas)
optim_GRU = optim.Adam(gru.parameters(),   lr=lr, betas=betas)


''' calc grad of models '''

def bp_i(inputs, y, retain=False):
    # y = 0.9 or 0 (one sided label smoothing). 0.9 is a guess
    dis_i.zero_grad()
    label.resize_(inputs.size(0)).fill_(y)
    labelv = Variable(label)
    outputs = dis_i(inputs)
    err = criterion(outputs, labelv)
    err.backward(retain_graph=retain)
    return err.data[0]

def bp_v(inputs, y, retain=False):
    dis_v.zero_grad()
    label.resize_(inputs.size(0)).fill_(y)
    labelv = Variable(label)
    outputs = dis_v(inputs)
    err = criterion(outputs, labelv)
    err.backward(retain_graph=retain)
    return err.data[0]


''' gen input noise for fake video '''

def gen_z(n_frames):
    z_C = Variable(torch.randn(batch_size, d_C))
    #  repeat z_C to (batch_size, n_frames, d_C)
    z_C = z_C.unsqueeze(1).repeat(1, n_frames, 1)
    eps = Variable(torch.randn(n_frames, batch_size, d_E))
    if cuda == True:
        z_C, eps = z_C.cuda(), eps.cuda()

    gru.initHidden(batch_size)
    # notice that 1st dim of gru outputs is seq_len, 2nd is batch_size
    z_M = gru(eps).transpose(1, 0)
    z = torch.cat((z_M, z_C), 2)  # z.size() => (batch_size, n_frames, nz)
    return z.view(batch_size, n_frames, nz, 1, 1)


''' train models '''

n_iter = 100
start_time = time.time()

for epoch in range(n_iter):
    ''' prepare real images '''
    # real_video.size() => (batch_size, nc, T, img_size, img_size)
    real_video = random_choice()
    if cuda == True:
        real_video = real_video.cuda()
    real_video = Variable(real_video)
    real_img = real_video[:, :, np.random.randint(0, T), :, :]

    ''' prepare fake images '''
    # note that n_frames is sampled from video length distribution
    n_frames = video_lengths[np.random.randint(0, n_videos)]
    Z = gen_z(n_frames)  # Z.size() => (batch_size, n_frames, nz, 1, 1)
    
    # trim => (batch_size, T, nz, 1, 1)
    Z = trim_noise(Z)

    # gen videos
    Z = Z.contiguous().view(batch_size*T, nz, 1, 1)
    fake_video = gen_i(Z)
    fake_video = fake_video.view(batch_size, T, nc, img_size, img_size)

    # reshape => (batch_size, nc, T, img_size, img_size)
    fake_video = fake_video.transpose(2, 1)

    # img sampling
    fake_img = fake_video[:, :, np.random.randint(0, T), :, :]


    ''' back prop for dis_v '''
    err_Dv_real = bp_v(real_video, 0.9)
    err_Dv_fake = bp_v(fake_video.detach(), 0) # detach(): avoid calc grad twice
    err_Dv = err_Dv_real + err_Dv_fake

    ''' back prop for dis_i '''
    err_Di_real = bp_i(real_img, 0.9)
    err_Di_fake = bp_i(fake_img.detach(), 0)
    err_Di = err_Di_real + err_Di_fake

    ''' train discriminators '''
    optim_Di.step()
    optim_Dv.step()

    ''' back prop for gen_i and gru using video '''
    gen_i.zero_grad()
    gru.zero_grad()
    # calc grad using video. notice retain=True to back prop twice
    err_Gv = bp_v(fake_video, 0.9, retain=True)

    ''' back prop for gen_i and gru using img '''
    # calc grad using images
    err_Gi = bp_i(fake_img, 0.9)

    ''' train gen_i and gru '''
    optim_Gi.step()
    optim_GRU.step()

    if (epoch+1) % 10 == 0:
        print('[%d/%d] (%s) Loss_Di: %.4f Loss_Dv: %.4f Loss_Gi: %.4f Loss_Gv: %.4f'
              % (epoch+1, n_iter, timeSince(start_time), err_Di, err_Dv, err_Gi, err_Gv))
