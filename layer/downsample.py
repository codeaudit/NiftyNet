# -*- coding: utf-8 -*-
import numpy as np
import tensorflow as tf

from utilities.misc_common import look_up_operations
from . import layer_util
from .base_layer import Layer

SUPPORTED_OP = {'AVG', 'MAX', 'CONSTANT'}
SUPPORTED_PADDING = {'SAME', 'VALID'}


class DownSampleLayer(Layer):
    def __init__(self,
                 func,
                 kernel_size,
                 stride,
                 padding='SAME',
                 name='pooling'):
        self.func = func.upper()
        self.layer_name = '{}_{}'.format(self.func.lower(), name)
        super(DownSampleLayer, self).__init__(name=self.layer_name)

        self.padding = padding.upper()
        look_up_operations(self.padding, SUPPORTED_PADDING)

        self.kernel_size = kernel_size
        self.stride = stride

    def layer_op(self, input_tensor):
        spatial_rank = layer_util.infer_spatial_rank(input_tensor)
        look_up_operations(self.func, SUPPORTED_OP)
        if self.func == 'CONSTANT':
            kernel_shape = np.hstack((
                [self.kernel_size] * spatial_rank, 1, 1)).flatten()
            np_kernel = layer_util.trivial_kernel(kernel_shape)
            kernel = tf.constant(np_kernel, dtype=tf.float32)
            output_tensor = [tf.expand_dims(x, -1)
                             for x in tf.unstack(input_tensor, axis=-1)]
            output_tensor = [tf.nn.convolution(
                input=inputs,
                filter=kernel,
                strides=[self.stride] * spatial_rank,
                padding=self.padding,
                name='conv') for inputs in output_tensor]
            output_tensor = tf.concat(output_tensor, axis=-1)
        else:
            output_tensor = tf.nn.pool(
                input=input_tensor,
                window_shape=[self.kernel_size] * spatial_rank,
                pooling_type=self.func,
                padding=self.padding,
                dilation_rate=[1] * spatial_rank,
                strides=[self.stride] * spatial_rank,
                name=self.layer_name)
        return output_tensor