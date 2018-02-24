from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import math

import numpy as np
from progressbar import ETA, Bar, Percentage, ProgressBar

from model.utils.interpolate import *
from model.utils.custom_ops import *
from model.utils.data_io import DataSet, saveSampleResults


class CoopNet(object):
    def __init__(self, num_epochs=200, image_size=64, batch_size=100, nTileRow=12, nTileCol=12, d_lr=0.001, g_lr=0.0001,
                 beta1=0.5, sigma=0.3, refsig=0.016, des_step_size=0.002, des_sample_steps=10, gen_step_size=0.1,
                 gen_sample_steps=0, net_type='object', log_step=10,
                 data_path='/tmp/data/', category='rock', output_dir='./output'):
        self.type = net_type
        self.num_epochs = num_epochs
        self.batch_size = batch_size
        self.image_size = image_size
        self.nTileRow = nTileRow
        self.nTileCol = nTileCol
        self.num_chain = nTileRow * nTileCol
        self.des_sample_steps = des_sample_steps
        self.gen_sample_steps = gen_sample_steps

        self.d_lr = d_lr
        self.g_lr = g_lr
        self.beta1 = beta1
        self.delta1 = des_step_size
        self.refsig = refsig
        self.delta2 = gen_step_size
        self.sigma = sigma

        self.data_path = os.path.join(data_path, category)
        self.log_step = log_step
        self.output_dir = os.path.join(output_dir, category)

        self.log_dir = os.path.join(self.output_dir, 'log')
        self.sample_dir = os.path.join(self.output_dir, 'synthesis')
        self.interp_dir = os.path.join(self.output_dir, 'interpolation')
        self.model_dir = os.path.join(self.output_dir, 'checkpoints')

        if tf.gfile.Exists(self.log_dir):
            tf.gfile.DeleteRecursively(self.log_dir)
        tf.gfile.MakeDirs(self.log_dir)

        if self.type == 'texture':
            self.z_size = 49
        elif self.type == 'object':
            self.z_size = 100
        elif self.type == 'object_small':
            self.z_size = 2

        self.syn = tf.placeholder(shape=[None, self.image_size, self.image_size, 3], dtype=tf.float32)
        self.obs = tf.placeholder(shape=[None, self.image_size, self.image_size, 3], dtype=tf.float32)
        self.z = tf.placeholder(shape=[None, self.z_size], dtype=tf.float32)

    def descriptor(self, inputs, reuse=False):
        with tf.variable_scope('des', reuse=reuse):
            if self.type == 'object':
                conv1 = conv2d(inputs, 64, kernal=(5, 5), strides=(2, 2), padding="SAME", activate_fn=leaky_relu,
                               name="conv1")

                conv2 = conv2d(conv1, 128, kernal=(3, 3), strides=(2, 2), padding="SAME", activate_fn=leaky_relu,
                               name="conv2")

                conv3 = conv2d(conv2, 256, kernal=(3, 3), strides=(1, 1), padding="SAME", activate_fn=leaky_relu,
                               name="conv3")

                fc = fully_connected(conv3, 100, name="fc")

                return fc
            else:
                return NotImplementedError

    def generator(self, inputs, reuse=False, is_training=True):
        with tf.variable_scope('gen', reuse=reuse):
            if self.type == 'object':
                inputs = tf.reshape(inputs, [-1, 1, 1, self.z_size])
                convt1 = convt2d(inputs, (None, self.image_size // 16, self.image_size // 16, 512), kernal=(4, 4)
                                 , strides=(1, 1), padding="VALID", name="convt1")
                convt1 = tf.contrib.layers.batch_norm(convt1, is_training=is_training)
                convt1 = leaky_relu(convt1)

                convt2 = convt2d(convt1, (None, self.image_size // 8, self.image_size // 8, 256), kernal=(5, 5)
                                 , strides=(2, 2), padding="SAME", name="convt2")
                convt2 = tf.contrib.layers.batch_norm(convt2, is_training=is_training)
                convt2 = leaky_relu(convt2)

                convt3 = convt2d(convt2, (None, self.image_size // 4, self.image_size // 4, 128), kernal=(5, 5)
                                 , strides=(2, 2), padding="SAME", name="convt3")
                convt3 = tf.contrib.layers.batch_norm(convt3, is_training=is_training)
                convt3 = leaky_relu(convt3)

                convt4 = convt2d(convt3, (None, self.image_size // 2, self.image_size // 2, 64), kernal=(5, 5)
                                 , strides=(2, 2), padding="SAME", name="convt4")
                convt4 = tf.contrib.layers.batch_norm(convt4, is_training=is_training)
                convt4 = leaky_relu(convt4)

                convt5 = convt2d(convt4, (None, self.image_size, self.image_size, 3), kernal=(5, 5)
                                 , strides=(2, 2), padding="SAME", name="convt5")
                convt5 = tf.nn.tanh(convt5)

                return convt5
            else:
                return NotImplementedError

    def langevin_dynamics_descriptor(self, sess, samples, gradient, batch_id):
        for i in xrange(self.des_sample_steps):
            noise = np.random.randn(*samples.shape)
            grad = sess.run(gradient, feed_dict={self.syn: samples})
            samples = samples - 0.5 * self.delta1 * self.delta1 * (samples / self.refsig / self.refsig - grad) \
                      + self.delta1 * noise
            self.pbar.update(batch_id * self.des_sample_steps + i + 1)
        return samples

    def langevin_dynamics_generator(self, sess, z, img, gradient, batch_id):
        for i in xrange(self.gen_sample_steps):
            noise = np.random.randn(*z.shape)
            grad = sess.run(gradient, feed_dict={self.obs: img, self.z: z})
            z = z - 0.5 * self.delta2 * self.delta2 * (z / self.refsig / self.refsig + grad) + self.delta2 * noise
            self.pbar.update(batch_id * self.gen_sample_steps + i + 1)
        return z

    def train(self, sess):

        gen_res = self.generator(self.z, reuse=False)

        obs_res = self.descriptor(self.obs, reuse=False)
        syn_res = self.descriptor(self.syn, reuse=True)
        sample_loss = tf.reduce_sum(syn_res)
        dLdI = tf.gradients(sample_loss, self.syn)[0]

        recon_err_mean, recon_err_update = tf.contrib.metrics.streaming_mean_squared_error(
            tf.reduce_mean(self.syn, axis=0), tf.reduce_mean(self.obs, axis=0))

        # Prepare training data
        train_data = DataSet(self.data_path, image_size=self.image_size)
        num_batches = int(math.ceil(len(train_data) / self.batch_size))

        # descriptor variables
        des_vars = [var for var in tf.trainable_variables() if var.name.startswith('des')]

        des_loss = tf.subtract(tf.reduce_mean(syn_res, axis=0), tf.reduce_mean(obs_res, axis=0))
        des_loss_mean, des_loss_update = tf.contrib.metrics.streaming_mean(des_loss)

        des_optim = tf.train.AdamOptimizer(self.d_lr, beta1=self.beta1)
        des_grads_vars = des_optim.compute_gradients(des_loss, var_list=des_vars)
        des_grads = [tf.reduce_mean(tf.abs(grad)) for (grad, var) in des_grads_vars if '/w' in var.name]
        # update by mean of gradients
        apply_d_grads = des_optim.apply_gradients(des_grads_vars)

        # generator variables
        gen_vars = [var for var in tf.trainable_variables() if var.name.startswith('gen')]

        gen_loss = tf.reduce_mean(1.0 / (2 * self.sigma * self.sigma) * tf.square(self.obs - gen_res), axis=0)
        gen_loss_mean, gen_loss_update = tf.contrib.metrics.streaming_mean(tf.reduce_mean(gen_loss))
        dLdZ = tf.gradients(gen_loss, self.z)[0]

        gen_optim = tf.train.AdamOptimizer(self.g_lr, beta1=self.beta1)
        gen_grads_vars = gen_optim.compute_gradients(gen_loss, var_list=gen_vars)
        gen_grads = [tf.reduce_mean(tf.abs(grad)) for (grad, var) in gen_grads_vars if '/w' in var.name]
        apply_g_grads = gen_optim.apply_gradients(gen_grads_vars)

        tf.summary.scalar('des_loss', des_loss_mean)
        tf.summary.scalar('gen_loss', gen_loss_mean)
        tf.summary.scalar('recon_err', recon_err_mean)

        summary_op = tf.summary.merge_all()

        # initialize training
        sess.run(tf.global_variables_initializer())
        sess.run(tf.local_variables_initializer())

        sample_results = np.random.randn(self.num_chain * num_batches, self.image_size, self.image_size, 3)

        saver = tf.train.Saver(max_to_keep=50)

        writer = tf.summary.FileWriter(self.log_dir, sess.graph)

        for epoch in xrange(self.num_epochs):

            widgets = ["Epoch #%d|" % epoch, Percentage(), Bar(), ETA()]
            self.pbar = ProgressBar(maxval=num_batches * (self.des_sample_steps + self.gen_sample_steps),
                                    widgets=widgets)
            self.pbar.start()

            for i in xrange(num_batches):
                obs_data = train_data[i * self.batch_size:min(len(train_data), (i + 1) * self.batch_size)]
                z_vec = np.random.randn(self.num_chain, self.z_size)
                # Step G0: generate X ~ N(0, 1)
                g_res = sess.run(gen_res, feed_dict={self.z: z_vec})
                # Step D1: obtain synthesized images Y
                syn = self.langevin_dynamics_descriptor(sess, g_res, dLdI, i)
                # Step G1: update X using Y as training image
                z_vec = self.langevin_dynamics_generator(sess, z_vec, syn, dLdZ, i)
                # Step D2: update D net
                sess.run([des_loss_update, apply_d_grads], feed_dict={self.obs: obs_data, self.syn: syn})
                # Step G2: update G net
                sess.run([gen_loss_update, apply_g_grads], feed_dict={self.obs: syn, self.z: z_vec})

                # Compute MSE
                sess.run(recon_err_update, feed_dict={self.obs: obs_data, self.syn: syn})

                sample_results[i * self.num_chain:(i + 1) * self.num_chain] = syn

                if i == 0 and epoch % self.log_step == 0:
                    if not os.path.exists(self.sample_dir):
                        os.makedirs(self.sample_dir)
                    saveSampleResults(syn, "%s/des%03d.png" % (self.sample_dir, epoch), col_num=self.nTileCol)
                    saveSampleResults(g_res, "%s/gen%03d.png" % (self.sample_dir, epoch), col_num=self.nTileCol)

            self.pbar.finish()

            [des_loss_avg, gen_loss_avg, mse, summary] = sess.run([des_loss_mean, gen_loss_mean,
                                                                   recon_err_mean,
                                                                   summary_op])

            print('Epoch #{:d}, descriptor loss: {:.4f},  generator loss: {:.4f}, Avg MSE: {:4.4f}'.format(epoch,
                                                                                                           des_loss_avg,
                                                                                                           gen_loss_avg,
                                                                                                           mse))
            writer.add_summary(summary, epoch)

            if epoch % self.log_step == 0:
                if not os.path.exists(self.model_dir):
                    os.makedirs(self.model_dir)
                saver.save(sess, "%s/%s" % (self.model_dir, 'model.ckpt'), global_step=epoch)

    def test(self, sess, ckpt, sample_size):
        assert (ckpt != None, 'no checkpoint provided.')

        gen_res = self.generator(self.z, reuse=False)

        num_batches = int(math.ceil(sample_size / self.num_chain))

        saver = tf.train.Saver()

        sess.run(tf.global_variables_initializer())
        saver.restore(sess, ckpt)
        print('Loading checkpoint {}.'.format(ckpt))

        test_dir = os.path.join(self.output_dir, 'test')
        if not os.path.exists(test_dir):
            os.makedirs(test_dir)

        for i in xrange(num_batches):
            z_vec = np.random.randn(min(sample_size, self.num_chain), self.z_size)
            g_res = sess.run(gen_res, feed_dict={self.z: z_vec})
            saveSampleResults(g_res, "%s/gen%03d.png" % (test_dir, i), col_num=self.nTileCol)

            # output interpolation results
            interp_z = linear_interpolator(z_vec, npairs=self.nTileRow, ninterp=self.nTileCol)
            interp = sess.run(gen_res, feed_dict={self.z: interp_z})
            saveSampleResults(interp, "%s/interp%03d.png" % (test_dir, i), col_num=self.nTileCol)
            sample_size = sample_size - self.num_chain