from __future__ import print_function

import os
import StringIO
import scipy.misc
import numpy as np
from glob import glob
from tqdm import trange
from itertools import chain
from collections import deque
from tensorflow.python.ops import variables

from models import *
from utils import save_image

def next(loader):
    return loader.next()[0].data.numpy()

def to_nhwc(image, data_format):
    if data_format == 'NCHW':
        new_image = nchw_to_nhwc(image)
    else:
        new_image = image
    return new_image

def to_nchw_numpy(image):
    if image.shape[3] in [1, 3]:
        new_image = image.transpose([0, 3, 1, 2])
    else:
        new_image = image
    return new_image

def norm_img(image, data_format=None):
    image = image/127.5 - 1.
    if data_format:
        image = to_nhwc(image, data_format)
    return image

def denorm_img(norm, data_format):
    return tf.clip_by_value(to_nhwc((norm + 1)*127.5, data_format), 0, 255)

def slerp(val, low, high):
    """Code from https://github.com/soumith/dcgan.torch/issues/14"""
    omega = np.arccos(np.clip(np.dot(low/np.linalg.norm(low), high/np.linalg.norm(high)), -1, 1))
    so = np.sin(omega)
    if so == 0:
        return (1.0-val) * low + val * high # L'Hopital's rule/LERP
    return np.sin((1.0-val)*omega) / so * low + np.sin(val*omega) / so * high

class Trainer(object):
    def __init__(self, config, data_loader):
        self.config = config
        self.data_loader = data_loader
        self.dataset = config.dataset

        self.beta1 = config.beta1
        self.beta2 = config.beta2
        self.optimizer = config.optimizer
        self.batch_size = config.batch_size

        self.step = tf.Variable(0, name='step', trainable=False)

        self.g_lr = tf.Variable(config.g_lr, name='g_lr')
        self.d_lr = tf.Variable(config.d_lr, name='d_lr')
        self.g_r_lr = tf.Variable(config.g_r_lr, name='g_r_lr')
        self.g_r_lr_update = tf.assign(self.g_r_lr,
                                               tf.maximum(self.g_r_lr * 0.5, config.lr_lower_boundary),
                                               name='g_r_lr_update')

        self.g_lr_update = tf.assign(self.g_lr, tf.maximum(self.g_lr * 0.5, config.lr_lower_boundary), name='g_lr_update')
        
        self.d_lr_update = tf.assign(self.d_lr, tf.maximum(self.d_lr * 0.5, config.lr_lower_boundary), name='d_lr_update')

        self.gamma = config.gamma
        self.lambda_k = config.lambda_k

        self.z_num = config.z_num
        self.conv_hidden_num = config.conv_hidden_num
        self.input_scale_size = config.input_scale_size

        self.model_dir = config.model_dir
        self.load_path = config.load_path

        self.use_gpu = config.use_gpu
        self.data_format = config.data_format

        _, height, width, self.channel = \
                get_conv_shape(self.data_loader, self.data_format)
        self.repeat_num = int(np.log2(height)) - 2

        self.start_step = 0
        self.log_step = config.log_step
        self.max_step = config.max_step
        self.save_step = config.save_step
        self.lr_update_step = config.lr_update_step

        self.is_train = config.is_train
        self.build_model2()
        
        if config.reverse:
            loadable = variables._all_saveable_objects()
            with tf.variable_scope('g_r_lr') as vs:
                self.g_r_lr = tf.Variable(config.g_r_lr, name='g_r_lr')
                self.g_r_lr_update = tf.assign(self.g_r_lr,
                                               tf.maximum(self.g_r_lr * 0.5, config.lr_lower_boundary),
                                               name='g_r_lr_update')
            g_r_vars = tf.contrib.framework.get_variables(vs)
            rev_vars = self.build_reverse_model()
            local_init_op = tf.variables_initializer(self.G_r_var+rev_vars+g_r_vars)
        else:
            loadable = None
            local_init_op = 0
        self.summary_writer = tf.summary.FileWriter(self.model_dir)
        
        self.saver = tf.train.Saver(var_list=loadable)
        sv = tf.train.Supervisor(logdir=self.model_dir,
                                is_chief=True,
                                local_init_op=local_init_op,
                                saver=self.saver,
                                summary_op=None,
                                summary_writer=self.summary_writer,
                                save_model_secs=300,
                                global_step=self.step,
                                ready_for_local_init_op=None)

        gpu_options = tf.GPUOptions(allow_growth=True)
        sess_config = tf.ConfigProto(allow_soft_placement=True,
                                    gpu_options=gpu_options)

        self.sess = sv.prepare_or_wait_for_session(config=sess_config)    

        if not self.is_train:
            # dirty way to bypass graph finilization error
            g = tf.get_default_graph()
            g._finalized = False

            self.build_test_model()


    def train_reverse(self):
        for step in trange(self.start_step, self.max_step):
            fetch_dict = {
                "g_r_optim": self.g_r_optim,
            }
            if step % self.log_step == 0:
                fetch_dict.update({
                    "summary": self.summary_op_r,
                    "g_r_loss": self.g_r_loss,
                    "g_r_lossl1": self.g_r_lossl1,
                })
            result = self.sess.run(fetch_dict)
            if step % self.log_step == 0:
                self.summary_writer.add_summary(result['summary'], step)
                self.summary_writer.flush()
                
                g_r_loss = result['g_r_loss']
                g_r_lossl1 = result['g_r_lossl1']

                print("[{}/{}] Loss_G_r: {:.6f} Loss_G_r_l1 {:.6f}". \
                      format(step, self.max_step, g_r_loss, g_r_lossl1))

            if step % self.lr_update_step == self.lr_update_step - 1:
                self.sess.run([self.g_r_lr_update])

    def train(self):
        z_fixed = np.random.uniform(-1, 1, size=(self.batch_size, self.z_num))

        x_fixed = self.get_image_from_loader()
        save_image(x_fixed, '{}/x_fixed.png'.format(self.model_dir))

        prev_measure = 1
        measure_history = deque([0]*self.lr_update_step, self.lr_update_step)

        for step in trange(self.start_step, self.max_step):
            fetch_dict = {
                "k_update": self.k_update,
                "measure": self.measure,
            }
            if step % self.log_step == 0:
                fetch_dict.update({
                    "summary": self.summary_op,
                    "g_loss": self.g_loss,
                    "g_r_loss": self.g_rr_loss,
                    "d_loss": self.d_loss,
                    "k_t": self.k_t,
                })
            result = self.sess.run(fetch_dict)

            measure = result['measure']
            measure_history.append(measure)

            if step % self.log_step == 0:
                self.summary_writer.add_summary(result['summary'], step)
                self.summary_writer.flush()

                g_loss = result['g_loss']
                g_r_loss = result['g_r_loss']
                d_loss = result['d_loss']
                k_t = result['k_t']

                print("[{}/{}] Loss_D: {:.6f} Loss_G: {:.6f} Loss_G_r: {:.6f} measure: {:.4f}, k_t: {:.4f}". \
                      format(step, self.max_step, d_loss, g_loss, g_r_loss, measure, k_t))

            if step % (self.log_step * 40) == 0:
                large_fake = None
                large_fake2 = None
                x_fake = None
                z_fixed_ = z_fixed
                for i in range(6):
                    x_fake = self.generate(self.G, 'G', z_fixed_, self.model_dir, idx=step)
                    x_fake2 = self.generate(self.G2, 'G2', z_fixed_, self.model_dir, idx=step)
                    if large_fake is None:
                        large_fake = x_fake
                    else:
                        large_fake = np.concatenate([large_fake, x_fake])
                    if large_fake2 is None:
                        large_fake2 = x_fake2
                    else:
                        large_fake2 = np.concatenate([large_fake2, x_fake2])
                    z_fixed_ = np.random.uniform(-1, 1, size=(self.batch_size, self.z_num))
                #print(large_fake.shape)
                f = '{}/{}_G.png'.format(self.model_dir, step)
                f2 = '{}/{}_G2.png'.format(self.model_dir, step)
                save_image(large_fake, f, nrow=8)
                save_image(large_fake2, f2, nrow=8)
                print("[*] Samples saved: {}".format(f))
                print("[*] Samples saved: {}".format(f2))
                self.autoencode(x_fixed, self.model_dir, idx=step, x_fake=x_fake)
                self.interpolate_live_G(z_fixed, self.model_dir, idx=step)

            if step % self.lr_update_step == self.lr_update_step - 1:
                self.sess.run([self.g_lr_update, self.d_lr_update])
                #cur_measure = np.mean(measure_history)
                #if cur_measure > prev_measure * 0.99:
                #prev_measure = cur_measure

    def build_model2(self):
        self.x = self.data_loader
        x = norm_img(self.x)

        self.z = tf.random_uniform(
                (tf.shape(x)[0], self.z_num), minval=-1.0, maxval=1.0)
        #k = tf.Print(self.z, [self.z[0]], message='base model: ')
        self.k_t = tf.Variable(0., trainable=False, name='k_t')

        G, self.G_var = GeneratorCNN(
                self.z, self.conv_hidden_num, self.channel,
                self.repeat_num, reuse=False, data_format=self.data_format)
        self.G_ = G

        G_r, self.G_r_var = GeneratorRCNN(G, self.channel, self.z_num, self.repeat_num,
                                          self.conv_hidden_num, self.data_format)

        G2, G2_var = GeneratorCNN(
            G_r, self.conv_hidden_num, self.channel, self.repeat_num,
            reuse=True, data_format=self.data_format)
        
        d_out, self.D_z, self.D_var = DiscriminatorCNN(
                tf.concat([G, x], 0), self.channel, self.z_num, self.repeat_num,
                self.conv_hidden_num, self.data_format)
        AE_G, AE_x = tf.split(d_out, 2)

        self.G = denorm_img(G, self.data_format)
        self.G2 = denorm_img(G2, self.data_format)
        self.AE_G, self.AE_x = denorm_img(AE_G, self.data_format), denorm_img(AE_x, self.data_format)

        if self.optimizer == 'adam':
            optimizer = tf.train.AdamOptimizer
        else:
            raise Exception("[!] Caution! Paper didn't use {} opimizer other than Adam".format(config.optimizer))

        g_optimizer, d_optimizer = optimizer(self.g_lr), optimizer(self.d_lr)
        g_rr_optimizer = optimizer(self.g_r_lr)

        self.d_loss_real = tf.reduce_mean(tf.abs(AE_x - x))
        self.d_loss_fake = tf.reduce_mean(tf.abs(AE_G - G))

        self.d_loss = self.d_loss_real - self.k_t * self.d_loss_fake
        self.g_loss = tf.reduce_mean(tf.abs(AE_G - G))
        self.g_rr_loss = tf.reduce_mean(tf.abs(G - tf.reverse(tf.transpose(G2, [0, 1, 3, 2]), [2])))

        d_optim = d_optimizer.minimize(self.d_loss, var_list=self.D_var)
        g_optim = g_optimizer.minimize(self.g_loss, global_step=self.step, var_list=self.G_var)
        g_rr_optim = g_rr_optimizer.minimize(self.g_rr_loss, var_list=self.G_r_var)

        self.balance = self.gamma * self.d_loss_real - self.g_loss
        self.measure = self.d_loss_real + tf.abs(self.balance)

        with tf.control_dependencies([d_optim, g_optim, g_rr_optim]):
            self.k_update = tf.assign(
                self.k_t, tf.clip_by_value(self.k_t + self.lambda_k * self.balance, 0, 1))

        self.summary_op = tf.summary.merge([
            tf.summary.image("G", self.G),
            tf.summary.image("G2", self.G2),
            tf.summary.image("AE_G", self.AE_G),
            tf.summary.image("AE_x", self.AE_x),

            tf.summary.scalar("loss/d_loss", self.d_loss),
            tf.summary.scalar("loss/d_loss_real", self.d_loss_real),
            tf.summary.scalar("loss/d_loss_fake", self.d_loss_fake),
            tf.summary.scalar("loss/g_loss", self.g_loss),
            tf.summary.scalar("loss/g_rr_loss", self.g_rr_loss),
            tf.summary.scalar("misc/measure", self.measure),
            tf.summary.scalar("misc/k_t", self.k_t),
            tf.summary.scalar("misc/d_lr", self.d_lr),
            tf.summary.scalar("misc/g_lr", self.g_lr),
            tf.summary.scalar("misc/g_r_lr", self.g_r_lr),
            tf.summary.scalar("misc/balance", self.balance),
        ])

    def build_model(self):
        self.x = self.data_loader
        x = norm_img(self.x)

        self.z = tf.random_uniform(
                (tf.shape(x)[0], self.z_num), minval=-1.0, maxval=1.0)
        #k = tf.Print(self.z, [self.z[0]], message='base model: ')
        self.k_t = tf.Variable(0., trainable=False, name='k_t')

        G, self.G_var = GeneratorCNN(
                self.z, self.conv_hidden_num, self.channel,
                self.repeat_num, reuse=False, data_format=self.data_format)
        self.G_ = G
        
        d_out, self.D_z, self.D_var = DiscriminatorCNN(
                tf.concat([G, x], 0), self.channel, self.z_num, self.repeat_num,
                self.conv_hidden_num, self.data_format)
        AE_G, AE_x = tf.split(d_out, 2)

        self.G = denorm_img(G, self.data_format)
        self.AE_G, self.AE_x = denorm_img(AE_G, self.data_format), denorm_img(AE_x, self.data_format)

        if self.optimizer == 'adam':
            optimizer = tf.train.AdamOptimizer
        else:
            raise Exception("[!] Caution! Paper didn't use {} opimizer other than Adam".format(config.optimizer))

        g_optimizer, d_optimizer = optimizer(self.g_lr), optimizer(self.d_lr)

        self.d_loss_real = tf.reduce_mean(tf.abs(AE_x - x))
        self.d_loss_fake = tf.reduce_mean(tf.abs(AE_G - G))

        self.d_loss = self.d_loss_real - self.k_t * self.d_loss_fake
        self.g_loss = tf.reduce_mean(tf.abs(AE_G - G))

        d_optim = d_optimizer.minimize(self.d_loss, var_list=self.D_var)
        g_optim = g_optimizer.minimize(self.g_loss, global_step=self.step, var_list=self.G_var)

        self.balance = self.gamma * self.d_loss_real - self.g_loss
        self.measure = self.d_loss_real + tf.abs(self.balance)

        with tf.control_dependencies([d_optim, g_optim]):
            self.k_update = tf.assign(
                self.k_t, tf.clip_by_value(self.k_t + self.lambda_k * self.balance, 0, 1))

        self.summary_op = tf.summary.merge([
            tf.summary.image("G", self.G),
            tf.summary.image("AE_G", self.AE_G),
            tf.summary.image("AE_x", self.AE_x),

            tf.summary.scalar("loss/d_loss", self.d_loss),
            tf.summary.scalar("loss/d_loss_real", self.d_loss_real),
            tf.summary.scalar("loss/d_loss_fake", self.d_loss_fake),
            tf.summary.scalar("loss/g_loss", self.g_loss),
            tf.summary.scalar("misc/measure", self.measure),
            tf.summary.scalar("misc/k_t", self.k_t),
            tf.summary.scalar("misc/d_lr", self.d_lr),
            tf.summary.scalar("misc/g_lr", self.g_lr),
            tf.summary.scalar("misc/balance", self.balance),
        ])

    def build_reverse_model(self):
        G_r, self.G_r_var = GeneratorRCNN(self.G_, self.channel, self.z_num, self.repeat_num,
                                          self.conv_hidden_num, self.data_format)
        with tf.variable_scope("build_reverse") as vs:
            g_r_optimizer = tf.train.AdamOptimizer(self.g_r_lr)
            #p = tf.Print(self.z, [self.z[0]], message='reverse_model')
            #self.g_r_loss = tf.reduce_mean(tf.square(G_r - self.z))/2.
            #p = tf.map_fn(lambda v: v.name, self.G_r_var)
            #print(self.G_r_var)
            self.g_r_loss = tf.reduce_mean(tf.square(G_r - self.z))/2.
            self.g_r_lossl1 = tf.reduce_mean(tf.abs(G_r - self.z))
            self.g_r_optim = g_r_optimizer.minimize(self.g_r_lossl1, self.step, var_list=self.G_r_var)

        self.summary_op_r = tf.summary.merge([
            tf.summary.scalar("loss/g_r_loss", self.g_r_loss),
            tf.summary.scalar("loss/g_r_lossl1", self.g_r_lossl1),
            tf.summary.scalar("misc/g_r_lr", self.g_r_lr),
        ])
        return tf.contrib.framework.get_variables(vs)

    def build_test_model(self):
        with tf.variable_scope("test") as vs:
            # Extra ops for interpolation
            z_optimizer = tf.train.AdamOptimizer(0.0001)

            self.z_r = tf.get_variable("z_r", [self.batch_size, self.z_num], tf.float32)
            self.z_r_update = tf.assign(self.z_r, self.z)

        G_z_r, _ = GeneratorCNN(
                self.z_r, self.conv_hidden_num, self.channel, self.repeat_num, self.data_format, reuse=True)

        with tf.variable_scope("test") as vs:
            self.z_r_loss = tf.reduce_mean(tf.abs(self.x - G_z_r))
            self.z_r_optim = z_optimizer.minimize(self.z_r_loss, var_list=[self.z_r])

        test_variables = tf.contrib.framework.get_variables(vs)
        self.sess.run(tf.variables_initializer(test_variables))

    def generate(self, model, name, inputs, root_path=None, path=None, idx=None, save=True):
        x = self.sess.run(model, {self.z: inputs})
        if path is None and save:
            path = os.path.join(root_path, '{}_{}.png'.format(idx, name))
            #save_image(x, path)
            #print("[*] Samples saved: {}".format(path))
        return x

    def autoencode(self, inputs, path, idx=None, x_fake=None):
        items = {
            'real': inputs,
            'fake': x_fake,
        }
        for key, img in items.items():
            if img is None:
                continue
            if img.shape[3] in [1, 3]:
                img = img.transpose([0, 3, 1, 2])

            x_path = os.path.join(path, '{}_D_{}.png'.format(idx, key))
            x = self.sess.run(self.AE_x, {self.x: img})
            save_image(x, x_path)
            print("[*] Samples saved: {}".format(x_path))

    def encode(self, inputs):
        if inputs.shape[3] in [1, 3]:
            inputs = inputs.transpose([0, 3, 1, 2])
        return self.sess.run(self.D_z, {self.x: inputs})

    def decode(self, z):
        return self.sess.run(self.AE_x, {self.D_z: z})

    def interpolate_live_G(self, z_fixed, root_path, idx):
        z_flex = np.random.uniform(-1, 1, size=(self.batch_size, self.z_num))
        generated = []
        for _, ratio in enumerate(np.linspace(0, 1, 10)):
            z = np.stack([slerp(ratio, r1, r2) for r1, r2 in zip(z_fixed, z_flex)])
            z_decode = self.generate(self.G, 'G', z, save=False)
            generated.append(z_decode)

        generated = np.stack(generated).transpose([1, 0, 2, 3, 4])

        all_img_num = np.prod(generated.shape[:2])
        batch_generated = np.reshape(generated, [all_img_num] + list(generated.shape[2:]))
        save_image(batch_generated, os.path.join(root_path, 'test{}_interp_G.png'.format(idx)), nrow=10)
    
    def interpolate_G(self, real_batch, step=0, root_path='.', train_epoch=0):
        batch_size = len(real_batch)
        half_batch_size = int(batch_size/2)

        self.sess.run(self.z_r_update)
        tf_real_batch = to_nchw_numpy(real_batch)
        for i in trange(train_epoch):
            z_r_loss, _ = self.sess.run([self.z_r_loss, self.z_r_optim], {self.x: tf_real_batch})
        z = self.sess.run(self.z_r)

        z1, z2 = z[:half_batch_size], z[half_batch_size:]
        real1_batch, real2_batch = real_batch[:half_batch_size], real_batch[half_batch_size:]

        generated = []
        for idx, ratio in enumerate(np.linspace(0, 1, 10)):
            z = np.stack([slerp(ratio, r1, r2) for r1, r2 in zip(z1, z2)])
            z_decode = self.generate(self.G, 'G', z, save=False)
            generated.append(z_decode)

        generated = np.stack(generated).transpose([1, 0, 2, 3, 4])
        for idx, img in enumerate(generated):
            save_image(img, os.path.join(root_path, 'test{}_interp_G_{}.png'.format(step, idx)), nrow=10)

        all_img_num = np.prod(generated.shape[:2])
        batch_generated = np.reshape(generated, [all_img_num] + list(generated.shape[2:]))
        save_image(batch_generated, os.path.join(root_path, 'test{}_interp_G.png'.format(step)), nrow=10)

    def interpolate_D(self, real1_batch, real2_batch, step=0, root_path="."):
        real1_encode = self.encode(real1_batch)
        real2_encode = self.encode(real2_batch)

        decodes = []
        for idx, ratio in enumerate(np.linspace(0, 1, 10)):
            z = np.stack([slerp(ratio, r1, r2) for r1, r2 in zip(real1_encode, real2_encode)])
            z_decode = self.decode(z)
            decodes.append(z_decode)

        decodes = np.stack(decodes).transpose([1, 0, 2, 3, 4])
        for idx, img in enumerate(decodes):
            img = np.concatenate([[real1_batch[idx]], img, [real2_batch[idx]]], 0)
            save_image(img, os.path.join(root_path, 'test{}_interp_D_{}.png'.format(step, idx)), nrow=10 + 2)

    def test(self):
        root_path = "./"#self.model_dir

        all_G_z = None
        for step in range(3):
            real1_batch = self.get_image_from_loader()
            real2_batch = self.get_image_from_loader()

            save_image(real1_batch, os.path.join(root_path, 'test{}_real1.png'.format(step)))
            save_image(real2_batch, os.path.join(root_path, 'test{}_real2.png'.format(step)))

            self.autoencode(
                    real1_batch, self.model_dir, idx=os.path.join(root_path, "test{}_real1".format(step)))
            self.autoencode(
                    real2_batch, self.model_dir, idx=os.path.join(root_path, "test{}_real2".format(step)))

            self.interpolate_G(real1_batch, step, root_path)
            #self.interpolate_D(real1_batch, real2_batch, step, root_path)

            z_fixed = np.random.uniform(-1, 1, size=(self.batch_size, self.z_num))
            G_z = self.generate(self.G, 'G', z_fixed, path=os.path.join(root_path, "test{}_G_z.png".format(step)))

            if all_G_z is None:
                all_G_z = G_z
            else:
                all_G_z = np.concatenate([all_G_z, G_z])
            save_image(all_G_z, '{}/G_z{}.png'.format(root_path, step))

        save_image(all_G_z, '{}/all_G_z.png'.format(root_path), nrow=16)

    def get_image_from_loader(self):
        x = self.data_loader.eval(session=self.sess)
        if self.data_format == 'NCHW':
            x = x.transpose([0, 2, 3, 1])
        return x
