# Copyright 2023 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Contains common building blocks for neural networks."""

from typing import Any, Callable, Dict, List, Optional, Tuple, Union, Text

# Import libraries
from absl import logging
import tensorflow as tf

from official.modeling import tf_utils
from official.nlp import modeling as nlp_modeling
from official.vision.modeling.layers import nn_layers


def _pad_strides(strides: int, axis: int) -> Tuple[int, int, int, int]:
  """Converts int to len 4 strides (`tf.nn.avg_pool` uses length 4)."""
  if axis == 1:
    return (1, 1, strides, strides)
  else:
    return (1, strides, strides, 1)


def _maybe_downsample(x: tf.Tensor, out_filter: int, strides: int,
                      axis: int) -> tf.Tensor:
  """Downsamples feature map and 0-pads tensor if in_filter != out_filter."""
  data_format = 'NCHW' if axis == 1 else 'NHWC'
  strides = _pad_strides(strides, axis=axis)

  x = tf.nn.avg_pool(x, strides, strides, 'VALID', data_format=data_format)

  in_filter = x.shape[axis]
  if in_filter < out_filter:
    # Pad on channel dimension with 0s: half on top half on bottom.
    pad_size = [(out_filter - in_filter) // 2, (out_filter - in_filter) // 2]
    if axis == 1:
      x = tf.pad(x, [[0, 0], pad_size, [0, 0], [0, 0]])
    else:
      x = tf.pad(x, [[0, 0], [0, 0], [0, 0], pad_size])

  return x + 0.


@tf.keras.utils.register_keras_serializable(package='Vision')
class ResidualBlock(tf.keras.layers.Layer):
  """A residual block."""

  def __init__(self,
               filters,
               strides,
               use_projection=False,
               se_ratio=None,
               resnetd_shortcut=False,
               stochastic_depth_drop_rate=None,
               kernel_initializer='VarianceScaling',
               kernel_regularizer=None,
               bias_regularizer=None,
               activation='relu',
               use_explicit_padding: bool = False,
               use_sync_bn=False,
               norm_momentum=0.99,
               norm_epsilon=0.001,
               bn_trainable=True,
               **kwargs):
    """Initializes a residual block with BN after convolutions.

    Args:
      filters: An `int` number of filters for the first two convolutions. Note
        that the third and final convolution will use 4 times as many filters.
      strides: An `int` block stride. If greater than 1, this block will
        ultimately downsample the input.
      use_projection: A `bool` for whether this block should use a projection
        shortcut (versus the default identity shortcut). This is usually `True`
        for the first block of a block group, which may change the number of
        filters and the resolution.
      se_ratio: A `float` or None. Ratio of the Squeeze-and-Excitation layer.
      resnetd_shortcut: A `bool` if True, apply the resnetd style modification
        to the shortcut connection. Not implemented in residual blocks.
      stochastic_depth_drop_rate: A `float` or None. if not None, drop rate for
        the stochastic depth layer.
      kernel_initializer: A `str` of kernel_initializer for convolutional
        layers.
      kernel_regularizer: A `tf.keras.regularizers.Regularizer` object for
        Conv2D. Default to None.
      bias_regularizer: A `tf.keras.regularizers.Regularizer` object for Conv2d.
        Default to None.
      activation: A `str` name of the activation function.
      use_explicit_padding: Use 'VALID' padding for convolutions, but prepad
        inputs so that the output dimensions are the same as if 'SAME' padding
        were used.
      use_sync_bn: A `bool`. If True, use synchronized batch normalization.
      norm_momentum: A `float` of normalization momentum for the moving average.
      norm_epsilon: A `float` added to variance to avoid dividing by zero.
      bn_trainable: A `bool` that indicates whether batch norm layers should be
        trainable. Default to True.
      **kwargs: Additional keyword arguments to be passed.
    """
    super(ResidualBlock, self).__init__(**kwargs)

    self._filters = filters
    self._strides = strides
    self._use_projection = use_projection
    self._se_ratio = se_ratio
    self._resnetd_shortcut = resnetd_shortcut
    self._use_explicit_padding = use_explicit_padding
    self._use_sync_bn = use_sync_bn
    self._activation = activation
    self._stochastic_depth_drop_rate = stochastic_depth_drop_rate
    self._kernel_initializer = kernel_initializer
    self._norm_momentum = norm_momentum
    self._norm_epsilon = norm_epsilon
    self._kernel_regularizer = kernel_regularizer
    self._bias_regularizer = bias_regularizer
    self._norm = tf.keras.layers.BatchNormalization

    if tf.keras.backend.image_data_format() == 'channels_last':
      self._bn_axis = -1
    else:
      self._bn_axis = 1
    self._activation_fn = tf_utils.get_activation(activation)
    self._bn_trainable = bn_trainable

  def build(self, input_shape):
    if self._use_projection:
      self._shortcut = tf.keras.layers.Conv2D(
          filters=self._filters,
          kernel_size=1,
          strides=self._strides,
          use_bias=False,
          kernel_initializer=tf_utils.clone_initializer(
              self._kernel_initializer),
          kernel_regularizer=self._kernel_regularizer,
          bias_regularizer=self._bias_regularizer)
      self._norm0 = self._norm(
          axis=self._bn_axis,
          momentum=self._norm_momentum,
          epsilon=self._norm_epsilon,
          trainable=self._bn_trainable,
          synchronized=self._use_sync_bn,
      )

    conv1_padding = 'same'
    # explicit padding here is added for centernet
    if self._use_explicit_padding:
      self._pad = tf.keras.layers.ZeroPadding2D(padding=(1, 1))
      conv1_padding = 'valid'

    self._conv1 = tf.keras.layers.Conv2D(
        filters=self._filters,
        kernel_size=3,
        strides=self._strides,
        padding=conv1_padding,
        use_bias=False,
        kernel_initializer=tf_utils.clone_initializer(self._kernel_initializer),
        kernel_regularizer=self._kernel_regularizer,
        bias_regularizer=self._bias_regularizer)
    self._norm1 = self._norm(
        axis=self._bn_axis,
        momentum=self._norm_momentum,
        epsilon=self._norm_epsilon,
        trainable=self._bn_trainable,
        synchronized=self._use_sync_bn,
    )

    self._conv2 = tf.keras.layers.Conv2D(
        filters=self._filters,
        kernel_size=3,
        strides=1,
        padding='same',
        use_bias=False,
        kernel_initializer=tf_utils.clone_initializer(self._kernel_initializer),
        kernel_regularizer=self._kernel_regularizer,
        bias_regularizer=self._bias_regularizer)
    self._norm2 = self._norm(
        axis=self._bn_axis,
        momentum=self._norm_momentum,
        epsilon=self._norm_epsilon,
        trainable=self._bn_trainable,
        synchronized=self._use_sync_bn,
    )

    if self._se_ratio and self._se_ratio > 0 and self._se_ratio <= 1:
      self._squeeze_excitation = nn_layers.SqueezeExcitation(
          in_filters=self._filters,
          out_filters=self._filters,
          se_ratio=self._se_ratio,
          kernel_initializer=tf_utils.clone_initializer(
              self._kernel_initializer),
          kernel_regularizer=self._kernel_regularizer,
          bias_regularizer=self._bias_regularizer)
    else:
      self._squeeze_excitation = None

    if self._stochastic_depth_drop_rate:
      self._stochastic_depth = nn_layers.StochasticDepth(
          self._stochastic_depth_drop_rate)
    else:
      self._stochastic_depth = None

    super(ResidualBlock, self).build(input_shape)

  def get_config(self):
    config = {
        'filters': self._filters,
        'strides': self._strides,
        'use_projection': self._use_projection,
        'se_ratio': self._se_ratio,
        'resnetd_shortcut': self._resnetd_shortcut,
        'stochastic_depth_drop_rate': self._stochastic_depth_drop_rate,
        'kernel_initializer': self._kernel_initializer,
        'kernel_regularizer': self._kernel_regularizer,
        'bias_regularizer': self._bias_regularizer,
        'activation': self._activation,
        'use_explicit_padding': self._use_explicit_padding,
        'use_sync_bn': self._use_sync_bn,
        'norm_momentum': self._norm_momentum,
        'norm_epsilon': self._norm_epsilon,
        'bn_trainable': self._bn_trainable
    }
    base_config = super(ResidualBlock, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))

  def call(self, inputs, training=None):
    shortcut = inputs
    if self._use_projection:
      shortcut = self._shortcut(shortcut)
      shortcut = self._norm0(shortcut)

    if self._use_explicit_padding:
      inputs = self._pad(inputs)
    x = self._conv1(inputs)
    x = self._norm1(x)
    x = self._activation_fn(x)

    x = self._conv2(x)
    x = self._norm2(x)

    if self._squeeze_excitation:
      x = self._squeeze_excitation(x)

    if self._stochastic_depth:
      x = self._stochastic_depth(x, training=training)

    return self._activation_fn(x + shortcut)


@tf.keras.utils.register_keras_serializable(package='Vision')
class BottleneckBlock(tf.keras.layers.Layer):
  """A standard bottleneck block."""

  def __init__(self,
               filters,
               strides,
               dilation_rate=1,
               use_projection=False,
               se_ratio=None,
               resnetd_shortcut=False,
               stochastic_depth_drop_rate=None,
               kernel_initializer='VarianceScaling',
               kernel_regularizer=None,
               bias_regularizer=None,
               activation='relu',
               use_sync_bn=False,
               norm_momentum=0.99,
               norm_epsilon=0.001,
               bn_trainable=True,
               **kwargs):
    """Initializes a standard bottleneck block with BN after convolutions.

    Args:
      filters: An `int` number of filters for the first two convolutions. Note
        that the third and final convolution will use 4 times as many filters.
      strides: An `int` block stride. If greater than 1, this block will
        ultimately downsample the input.
      dilation_rate: An `int` dilation_rate of convolutions. Default to 1.
      use_projection: A `bool` for whether this block should use a projection
        shortcut (versus the default identity shortcut). This is usually `True`
        for the first block of a block group, which may change the number of
        filters and the resolution.
      se_ratio: A `float` or None. Ratio of the Squeeze-and-Excitation layer.
      resnetd_shortcut: A `bool`. If True, apply the resnetd style modification
        to the shortcut connection.
      stochastic_depth_drop_rate: A `float` or None. If not None, drop rate for
        the stochastic depth layer.
      kernel_initializer: A `str` of kernel_initializer for convolutional
        layers.
      kernel_regularizer: A `tf.keras.regularizers.Regularizer` object for
        Conv2D. Default to None.
      bias_regularizer: A `tf.keras.regularizers.Regularizer` object for Conv2d.
        Default to None.
      activation: A `str` name of the activation function.
      use_sync_bn: A `bool`. If True, use synchronized batch normalization.
      norm_momentum: A `float` of normalization momentum for the moving average.
      norm_epsilon: A `float` added to variance to avoid dividing by zero.
      bn_trainable: A `bool` that indicates whether batch norm layers should be
        trainable. Default to True.
      **kwargs: Additional keyword arguments to be passed.
    """
    super(BottleneckBlock, self).__init__(**kwargs)

    self._filters = filters
    self._strides = strides
    self._dilation_rate = dilation_rate
    self._use_projection = use_projection
    self._se_ratio = se_ratio
    self._resnetd_shortcut = resnetd_shortcut
    self._use_sync_bn = use_sync_bn
    self._activation = activation
    self._stochastic_depth_drop_rate = stochastic_depth_drop_rate
    self._kernel_initializer = kernel_initializer
    self._norm_momentum = norm_momentum
    self._norm_epsilon = norm_epsilon
    self._kernel_regularizer = kernel_regularizer
    self._bias_regularizer = bias_regularizer
    self._norm = tf.keras.layers.BatchNormalization

    if tf.keras.backend.image_data_format() == 'channels_last':
      self._bn_axis = -1
    else:
      self._bn_axis = 1
    self._bn_trainable = bn_trainable

  def build(self, input_shape):
    if self._use_projection:
      if self._resnetd_shortcut:
        self._shortcut0 = tf.keras.layers.AveragePooling2D(
            pool_size=2, strides=self._strides, padding='same')
        self._shortcut1 = tf.keras.layers.Conv2D(
            filters=self._filters * 4,
            kernel_size=1,
            strides=1,
            use_bias=False,
            kernel_initializer=tf_utils.clone_initializer(
                self._kernel_initializer),
            kernel_regularizer=self._kernel_regularizer,
            bias_regularizer=self._bias_regularizer)
      else:
        self._shortcut = tf.keras.layers.Conv2D(
            filters=self._filters * 4,
            kernel_size=1,
            strides=self._strides,
            use_bias=False,
            kernel_initializer=tf_utils.clone_initializer(
                self._kernel_initializer),
            kernel_regularizer=self._kernel_regularizer,
            bias_regularizer=self._bias_regularizer)

      self._norm0 = self._norm(
          axis=self._bn_axis,
          momentum=self._norm_momentum,
          epsilon=self._norm_epsilon,
          trainable=self._bn_trainable,
          synchronized=self._use_sync_bn,
      )

    self._conv1 = tf.keras.layers.Conv2D(
        filters=self._filters,
        kernel_size=1,
        strides=1,
        use_bias=False,
        kernel_initializer=tf_utils.clone_initializer(self._kernel_initializer),
        kernel_regularizer=self._kernel_regularizer,
        bias_regularizer=self._bias_regularizer)
    self._norm1 = self._norm(
        axis=self._bn_axis,
        momentum=self._norm_momentum,
        epsilon=self._norm_epsilon,
        trainable=self._bn_trainable,
        synchronized=self._use_sync_bn,
    )
    self._activation1 = tf_utils.get_activation(
        self._activation, use_keras_layer=True)

    self._conv2 = tf.keras.layers.Conv2D(
        filters=self._filters,
        kernel_size=3,
        strides=self._strides,
        dilation_rate=self._dilation_rate,
        padding='same',
        use_bias=False,
        kernel_initializer=tf_utils.clone_initializer(self._kernel_initializer),
        kernel_regularizer=self._kernel_regularizer,
        bias_regularizer=self._bias_regularizer)
    self._norm2 = self._norm(
        axis=self._bn_axis,
        momentum=self._norm_momentum,
        epsilon=self._norm_epsilon,
        trainable=self._bn_trainable,
        synchronized=self._use_sync_bn,
    )
    self._activation2 = tf_utils.get_activation(
        self._activation, use_keras_layer=True)

    self._conv3 = tf.keras.layers.Conv2D(
        filters=self._filters * 4,
        kernel_size=1,
        strides=1,
        use_bias=False,
        kernel_initializer=tf_utils.clone_initializer(self._kernel_initializer),
        kernel_regularizer=self._kernel_regularizer,
        bias_regularizer=self._bias_regularizer)
    self._norm3 = self._norm(
        axis=self._bn_axis,
        momentum=self._norm_momentum,
        epsilon=self._norm_epsilon,
        trainable=self._bn_trainable,
        synchronized=self._use_sync_bn,
    )
    self._activation3 = tf_utils.get_activation(
        self._activation, use_keras_layer=True)

    if self._se_ratio and self._se_ratio > 0 and self._se_ratio <= 1:
      self._squeeze_excitation = nn_layers.SqueezeExcitation(
          in_filters=self._filters * 4,
          out_filters=self._filters * 4,
          se_ratio=self._se_ratio,
          kernel_initializer=tf_utils.clone_initializer(
              self._kernel_initializer),
          kernel_regularizer=self._kernel_regularizer,
          bias_regularizer=self._bias_regularizer)
    else:
      self._squeeze_excitation = None

    if self._stochastic_depth_drop_rate:
      self._stochastic_depth = nn_layers.StochasticDepth(
          self._stochastic_depth_drop_rate)
    else:
      self._stochastic_depth = None
    self._add = tf.keras.layers.Add()

    super(BottleneckBlock, self).build(input_shape)

  def get_config(self):
    config = {
        'filters': self._filters,
        'strides': self._strides,
        'dilation_rate': self._dilation_rate,
        'use_projection': self._use_projection,
        'se_ratio': self._se_ratio,
        'resnetd_shortcut': self._resnetd_shortcut,
        'stochastic_depth_drop_rate': self._stochastic_depth_drop_rate,
        'kernel_initializer': self._kernel_initializer,
        'kernel_regularizer': self._kernel_regularizer,
        'bias_regularizer': self._bias_regularizer,
        'activation': self._activation,
        'use_sync_bn': self._use_sync_bn,
        'norm_momentum': self._norm_momentum,
        'norm_epsilon': self._norm_epsilon,
        'bn_trainable': self._bn_trainable
    }
    base_config = super(BottleneckBlock, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))

  def call(self, inputs, training=None):
    shortcut = inputs
    if self._use_projection:
      if self._resnetd_shortcut:
        shortcut = self._shortcut0(shortcut)
        shortcut = self._shortcut1(shortcut)
      else:
        shortcut = self._shortcut(shortcut)
      shortcut = self._norm0(shortcut)

    x = self._conv1(inputs)
    x = self._norm1(x)
    x = self._activation1(x)

    x = self._conv2(x)
    x = self._norm2(x)
    x = self._activation2(x)

    x = self._conv3(x)
    x = self._norm3(x)

    if self._squeeze_excitation:
      x = self._squeeze_excitation(x)

    if self._stochastic_depth:
      x = self._stochastic_depth(x, training=training)

    x = self._add([x, shortcut])
    return self._activation3(x)


@tf.keras.utils.register_keras_serializable(package='Vision')
class InvertedBottleneckBlock(tf.keras.layers.Layer):
  """An inverted bottleneck block."""

  def __init__(self,
               in_filters,
               out_filters,
               expand_ratio,
               strides,
               kernel_size=3,
               se_ratio=None,
               stochastic_depth_drop_rate=None,
               kernel_initializer='VarianceScaling',
               kernel_regularizer=None,
               bias_regularizer=None,
               activation='relu',
               se_inner_activation='relu',
               se_gating_activation='sigmoid',
               se_round_down_protect=True,
               expand_se_in_filters=False,
               depthwise_activation=None,
               use_sync_bn=False,
               dilation_rate=1,
               divisible_by=1,
               regularize_depthwise=False,
               use_depthwise=True,
               use_residual=True,
               norm_momentum=0.99,
               norm_epsilon=0.001,
               output_intermediate_endpoints=False,
               **kwargs):
    """Initializes an inverted bottleneck block with BN after convolutions.

    Args:
      in_filters: An `int` number of filters of the input tensor.
      out_filters: An `int` number of filters of the output tensor.
      expand_ratio: An `int` of expand_ratio for an inverted bottleneck block.
      strides: An `int` block stride. If greater than 1, this block will
        ultimately downsample the input.
      kernel_size: An `int` kernel_size of the depthwise conv layer.
      se_ratio: A `float` or None. If not None, se ratio for the squeeze and
        excitation layer.
      stochastic_depth_drop_rate: A `float` or None. if not None, drop rate for
        the stochastic depth layer.
      kernel_initializer: A `str` of kernel_initializer for convolutional
        layers.
      kernel_regularizer: A `tf.keras.regularizers.Regularizer` object for
        Conv2D. Default to None.
      bias_regularizer: A `tf.keras.regularizers.Regularizer` object for Conv2d.
        Default to None.
      activation: A `str` name of the activation function.
      se_inner_activation: A `str` name of squeeze-excitation inner activation.
      se_gating_activation: A `str` name of squeeze-excitation gating
        activation.
      se_round_down_protect: A `bool` of whether round down more than 10% will
        be allowed in SE layer.
      expand_se_in_filters: A `bool` of whether or not to expand in_filter in
        squeeze and excitation layer.
      depthwise_activation: A `str` name of the activation function for
        depthwise only.
      use_sync_bn: A `bool`. If True, use synchronized batch normalization.
      dilation_rate: An `int` that specifies the dilation rate to use for.
      divisible_by: An `int` that ensures all inner dimensions are divisible by
        this number. dilated convolution: An `int` to specify the same value for
        all spatial dimensions.
      regularize_depthwise: A `bool` of whether or not apply regularization on
        depthwise.
      use_depthwise: A `bool` of whether to uses fused convolutions instead of
        depthwise.
      use_residual: A `bool` of whether to include residual connection between
        input and output.
      norm_momentum: A `float` of normalization momentum for the moving average.
      norm_epsilon: A `float` added to variance to avoid dividing by zero.
      output_intermediate_endpoints: A `bool` of whether or not output the
        intermediate endpoints.
      **kwargs: Additional keyword arguments to be passed.
    """
    super(InvertedBottleneckBlock, self).__init__(**kwargs)

    self._in_filters = in_filters
    self._out_filters = out_filters
    self._expand_ratio = expand_ratio
    self._strides = strides
    self._kernel_size = kernel_size
    self._se_ratio = se_ratio
    self._divisible_by = divisible_by
    self._stochastic_depth_drop_rate = stochastic_depth_drop_rate
    self._dilation_rate = dilation_rate
    self._use_sync_bn = use_sync_bn
    self._regularize_depthwise = regularize_depthwise
    self._use_depthwise = use_depthwise
    self._use_residual = use_residual
    self._activation = activation
    self._se_inner_activation = se_inner_activation
    self._se_gating_activation = se_gating_activation
    self._depthwise_activation = depthwise_activation
    self._se_round_down_protect = se_round_down_protect
    self._kernel_initializer = kernel_initializer
    self._norm_momentum = norm_momentum
    self._norm_epsilon = norm_epsilon
    self._kernel_regularizer = kernel_regularizer
    self._bias_regularizer = bias_regularizer
    self._expand_se_in_filters = expand_se_in_filters
    self._output_intermediate_endpoints = output_intermediate_endpoints
    self._norm = tf.keras.layers.BatchNormalization

    if tf.keras.backend.image_data_format() == 'channels_last':
      self._bn_axis = -1
    else:
      self._bn_axis = 1
    if not depthwise_activation:
      self._depthwise_activation = activation
    if regularize_depthwise:
      self._depthsize_regularizer = kernel_regularizer
    else:
      self._depthsize_regularizer = None

  def build(self, input_shape):
    expand_filters = self._in_filters
    if self._expand_ratio > 1:
      # First 1x1 conv for channel expansion.
      expand_filters = nn_layers.make_divisible(
          self._in_filters * self._expand_ratio, self._divisible_by)

      expand_kernel = 1 if self._use_depthwise else self._kernel_size
      expand_stride = 1 if self._use_depthwise else self._strides

      self._conv0 = tf.keras.layers.Conv2D(
          filters=expand_filters,
          kernel_size=expand_kernel,
          strides=expand_stride,
          padding='same',
          use_bias=False,
          kernel_initializer=tf_utils.clone_initializer(
              self._kernel_initializer),
          kernel_regularizer=self._kernel_regularizer,
          bias_regularizer=self._bias_regularizer)
      self._norm0 = self._norm(
          axis=self._bn_axis,
          momentum=self._norm_momentum,
          epsilon=self._norm_epsilon,
          synchronized=self._use_sync_bn,
      )
      self._activation_layer = tf_utils.get_activation(
          self._activation, use_keras_layer=True)

    if self._use_depthwise:
      # Depthwise conv.
      self._conv1 = tf.keras.layers.DepthwiseConv2D(
          kernel_size=(self._kernel_size, self._kernel_size),
          strides=self._strides,
          padding='same',
          depth_multiplier=1,
          dilation_rate=self._dilation_rate,
          use_bias=False,
          depthwise_initializer=tf_utils.clone_initializer(
              self._kernel_initializer),
          depthwise_regularizer=self._depthsize_regularizer,
          bias_regularizer=self._bias_regularizer)
      self._norm1 = self._norm(
          axis=self._bn_axis,
          momentum=self._norm_momentum,
          epsilon=self._norm_epsilon,
          synchronized=self._use_sync_bn,
      )
      self._depthwise_activation_layer = tf_utils.get_activation(
          self._depthwise_activation, use_keras_layer=True)

    # Squeeze and excitation.
    if self._se_ratio and self._se_ratio > 0 and self._se_ratio <= 1:
      logging.info('Use Squeeze and excitation.')
      in_filters = self._in_filters
      if self._expand_se_in_filters:
        in_filters = expand_filters
      self._squeeze_excitation = nn_layers.SqueezeExcitation(
          in_filters=in_filters,
          out_filters=expand_filters,
          se_ratio=self._se_ratio,
          divisible_by=self._divisible_by,
          round_down_protect=self._se_round_down_protect,
          kernel_initializer=tf_utils.clone_initializer(
              self._kernel_initializer),
          kernel_regularizer=self._kernel_regularizer,
          bias_regularizer=self._bias_regularizer,
          activation=self._se_inner_activation,
          gating_activation=self._se_gating_activation)
    else:
      self._squeeze_excitation = None

    # Last 1x1 conv.
    self._conv2 = tf.keras.layers.Conv2D(
        filters=self._out_filters,
        kernel_size=1,
        strides=1,
        padding='same',
        use_bias=False,
        kernel_initializer=tf_utils.clone_initializer(self._kernel_initializer),
        kernel_regularizer=self._kernel_regularizer,
        bias_regularizer=self._bias_regularizer)
    self._norm2 = self._norm(
        axis=self._bn_axis,
        momentum=self._norm_momentum,
        epsilon=self._norm_epsilon,
        synchronized=self._use_sync_bn,
    )

    if self._stochastic_depth_drop_rate:
      self._stochastic_depth = nn_layers.StochasticDepth(
          self._stochastic_depth_drop_rate)
    else:
      self._stochastic_depth = None
    self._add = tf.keras.layers.Add()

    super(InvertedBottleneckBlock, self).build(input_shape)

  def get_config(self):
    config = {
        'in_filters': self._in_filters,
        'out_filters': self._out_filters,
        'expand_ratio': self._expand_ratio,
        'strides': self._strides,
        'kernel_size': self._kernel_size,
        'se_ratio': self._se_ratio,
        'divisible_by': self._divisible_by,
        'stochastic_depth_drop_rate': self._stochastic_depth_drop_rate,
        'kernel_initializer': self._kernel_initializer,
        'kernel_regularizer': self._kernel_regularizer,
        'bias_regularizer': self._bias_regularizer,
        'activation': self._activation,
        'se_inner_activation': self._se_inner_activation,
        'se_gating_activation': self._se_gating_activation,
        'se_round_down_protect': self._se_round_down_protect,
        'expand_se_in_filters': self._expand_se_in_filters,
        'depthwise_activation': self._depthwise_activation,
        'dilation_rate': self._dilation_rate,
        'use_sync_bn': self._use_sync_bn,
        'regularize_depthwise': self._regularize_depthwise,
        'use_depthwise': self._use_depthwise,
        'use_residual': self._use_residual,
        'norm_momentum': self._norm_momentum,
        'norm_epsilon': self._norm_epsilon,
        'output_intermediate_endpoints': self._output_intermediate_endpoints
    }
    base_config = super(InvertedBottleneckBlock, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))

  def call(self, inputs, training=None):
    endpoints = {}
    shortcut = inputs
    if self._expand_ratio > 1:
      x = self._conv0(inputs)
      x = self._norm0(x)
      x = self._activation_layer(x)
    else:
      x = inputs

    if self._use_depthwise:
      x = self._conv1(x)
      x = self._norm1(x)
      x = self._depthwise_activation_layer(x)
      if self._output_intermediate_endpoints:
        endpoints['depthwise'] = x

    if self._squeeze_excitation:
      x = self._squeeze_excitation(x)

    x = self._conv2(x)
    x = self._norm2(x)

    if (self._use_residual and self._in_filters == self._out_filters and
        self._strides == 1):
      if self._stochastic_depth:
        x = self._stochastic_depth(x, training=training)
      x = self._add([x, shortcut])

    if self._output_intermediate_endpoints:
      return x, endpoints
    return x


@tf.keras.utils.register_keras_serializable(package='Vision')
class ResidualInner(tf.keras.layers.Layer):
  """Creates a single inner block of a residual.

  This corresponds to `F`/`G` functions in the RevNet paper:
  Aidan N. Gomez, Mengye Ren, Raquel Urtasun, Roger B. Grosse.
  The Reversible Residual Network: Backpropagation Without Storing Activations.
  (https://arxiv.org/pdf/1707.04585.pdf)
  """

  def __init__(
      self,
      filters: int,
      strides: int,
      kernel_initializer: Union[str, Callable[
          ..., tf.keras.initializers.Initializer]] = 'VarianceScaling',
      kernel_regularizer: Optional[tf.keras.regularizers.Regularizer] = None,
      activation: Union[str, Callable[..., tf.Tensor]] = 'relu',
      use_sync_bn: bool = False,
      norm_momentum: float = 0.99,
      norm_epsilon: float = 0.001,
      batch_norm_first: bool = True,
      **kwargs):
    """Initializes a ResidualInner.

    Args:
      filters: An `int` of output filter size.
      strides: An `int` of stride size for convolution for the residual block.
      kernel_initializer: A `str` or `tf.keras.initializers.Initializer`
        instance for convolutional layers.
      kernel_regularizer: A `tf.keras.regularizers.Regularizer` for Conv2D.
      activation: A `str` or `callable` instance of the activation function.
      use_sync_bn: A `bool`. If True, use synchronized batch normalization.
      norm_momentum: A `float` of normalization momentum for the moving average.
      norm_epsilon: A `float` added to variance to avoid dividing by zero.
      batch_norm_first: A `bool` of whether to apply activation and batch norm
        before conv.
      **kwargs: Additional keyword arguments to be passed.
    """
    super(ResidualInner, self).__init__(**kwargs)

    self.strides = strides
    self.filters = filters
    self._kernel_initializer = tf.keras.initializers.get(kernel_initializer)
    self._kernel_regularizer = kernel_regularizer
    self._activation = tf.keras.activations.get(activation)
    self._use_sync_bn = use_sync_bn
    self._norm_momentum = norm_momentum
    self._norm_epsilon = norm_epsilon
    self._batch_norm_first = batch_norm_first
    self._norm = tf.keras.layers.BatchNormalization

    if tf.keras.backend.image_data_format() == 'channels_last':
      self._bn_axis = -1
    else:
      self._bn_axis = 1
    self._activation_fn = tf_utils.get_activation(activation)

  def build(self, input_shape: tf.TensorShape):
    if self._batch_norm_first:
      self._batch_norm_0 = self._norm(
          axis=self._bn_axis,
          momentum=self._norm_momentum,
          epsilon=self._norm_epsilon,
          synchronized=self._use_sync_bn,
      )

    self._conv2d_1 = tf.keras.layers.Conv2D(
        filters=self.filters,
        kernel_size=3,
        strides=self.strides,
        use_bias=False,
        padding='same',
        kernel_initializer=tf_utils.clone_initializer(self._kernel_initializer),
        kernel_regularizer=self._kernel_regularizer)

    self._batch_norm_1 = self._norm(
        axis=self._bn_axis,
        momentum=self._norm_momentum,
        epsilon=self._norm_epsilon,
        synchronized=self._use_sync_bn,
    )

    self._conv2d_2 = tf.keras.layers.Conv2D(
        filters=self.filters,
        kernel_size=3,
        strides=1,
        use_bias=False,
        padding='same',
        kernel_initializer=tf_utils.clone_initializer(self._kernel_initializer),
        kernel_regularizer=self._kernel_regularizer)

    super(ResidualInner, self).build(input_shape)

  def get_config(self) -> Dict[str, Any]:
    config = {
        'filters': self.filters,
        'strides': self.strides,
        'kernel_initializer': self._kernel_initializer,
        'kernel_regularizer': self._kernel_regularizer,
        'activation': self._activation,
        'use_sync_bn': self._use_sync_bn,
        'norm_momentum': self._norm_momentum,
        'norm_epsilon': self._norm_epsilon,
        'batch_norm_first': self._batch_norm_first,
    }
    base_config = super(ResidualInner, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))

  def call(self,
           inputs: tf.Tensor,
           training: Optional[bool] = None) -> tf.Tensor:
    x = inputs
    if self._batch_norm_first:
      x = self._batch_norm_0(x, training=training)
      x = self._activation_fn(x)
    x = self._conv2d_1(x)

    x = self._batch_norm_1(x, training=training)
    x = self._activation_fn(x)
    x = self._conv2d_2(x)
    return x


@tf.keras.utils.register_keras_serializable(package='Vision')
class BottleneckResidualInner(tf.keras.layers.Layer):
  """Creates a single inner block of a bottleneck.

  This corresponds to `F`/`G` functions in the RevNet paper:
  Aidan N. Gomez, Mengye Ren, Raquel Urtasun, Roger B. Grosse.
  The Reversible Residual Network: Backpropagation Without Storing Activations.
  (https://arxiv.org/pdf/1707.04585.pdf)
  """

  def __init__(
      self,
      filters: int,
      strides: int,
      kernel_initializer: Union[str, Callable[
          ..., tf.keras.initializers.Initializer]] = 'VarianceScaling',
      kernel_regularizer: Optional[tf.keras.regularizers.Regularizer] = None,
      activation: Union[str, Callable[..., tf.Tensor]] = 'relu',
      use_sync_bn: bool = False,
      norm_momentum: float = 0.99,
      norm_epsilon: float = 0.001,
      batch_norm_first: bool = True,
      **kwargs):
    """Initializes a BottleneckResidualInner.

    Args:
      filters: An `int` number of filters for first 2 convolutions. Last Last,
        and thus the number of output channels from the bottlneck block is
        `4*filters`
      strides: An `int` of stride size for convolution for the residual block.
      kernel_initializer: A `str` or `tf.keras.initializers.Initializer`
        instance for convolutional layers.
      kernel_regularizer: A `tf.keras.regularizers.Regularizer` for Conv2D.
      activation: A `str` or `callable` instance of the activation function.
      use_sync_bn: A `bool`. If True, use synchronized batch normalization.
      norm_momentum: A `float` of normalization momentum for the moving average.
      norm_epsilon: A `float` added to variance to avoid dividing by zero.
      batch_norm_first: A `bool` of whether to apply activation and batch norm
        before conv.
      **kwargs: Additional keyword arguments to be passed.
    """
    super(BottleneckResidualInner, self).__init__(**kwargs)

    self.strides = strides
    self.filters = filters
    self._kernel_initializer = tf.keras.initializers.get(kernel_initializer)
    self._kernel_regularizer = kernel_regularizer
    self._activation = tf.keras.activations.get(activation)
    self._use_sync_bn = use_sync_bn
    self._norm_momentum = norm_momentum
    self._norm_epsilon = norm_epsilon
    self._batch_norm_first = batch_norm_first
    self._norm = tf.keras.layers.BatchNormalization

    if tf.keras.backend.image_data_format() == 'channels_last':
      self._bn_axis = -1
    else:
      self._bn_axis = 1
    self._activation_fn = tf_utils.get_activation(activation)

  def build(self, input_shape: tf.TensorShape):
    if self._batch_norm_first:
      self._batch_norm_0 = self._norm(
          axis=self._bn_axis,
          momentum=self._norm_momentum,
          epsilon=self._norm_epsilon,
          synchronized=self._use_sync_bn,
      )
    self._conv2d_1 = tf.keras.layers.Conv2D(
        filters=self.filters,
        kernel_size=1,
        strides=self.strides,
        use_bias=False,
        padding='same',
        kernel_initializer=tf_utils.clone_initializer(self._kernel_initializer),
        kernel_regularizer=self._kernel_regularizer)
    self._batch_norm_1 = self._norm(
        axis=self._bn_axis,
        momentum=self._norm_momentum,
        epsilon=self._norm_epsilon,
        synchronized=self._use_sync_bn,
    )
    self._conv2d_2 = tf.keras.layers.Conv2D(
        filters=self.filters,
        kernel_size=3,
        strides=1,
        use_bias=False,
        padding='same',
        kernel_initializer=tf_utils.clone_initializer(self._kernel_initializer),
        kernel_regularizer=self._kernel_regularizer)
    self._batch_norm_2 = self._norm(
        axis=self._bn_axis,
        momentum=self._norm_momentum,
        epsilon=self._norm_epsilon,
        synchronized=self._use_sync_bn,
    )
    self._conv2d_3 = tf.keras.layers.Conv2D(
        filters=self.filters * 4,
        kernel_size=1,
        strides=1,
        use_bias=False,
        padding='same',
        kernel_initializer=tf_utils.clone_initializer(self._kernel_initializer),
        kernel_regularizer=self._kernel_regularizer)

    super(BottleneckResidualInner, self).build(input_shape)

  def get_config(self) -> Dict[str, Any]:
    config = {
        'filters': self.filters,
        'strides': self.strides,
        'kernel_initializer': self._kernel_initializer,
        'kernel_regularizer': self._kernel_regularizer,
        'activation': self._activation,
        'use_sync_bn': self._use_sync_bn,
        'norm_momentum': self._norm_momentum,
        'norm_epsilon': self._norm_epsilon,
        'batch_norm_first': self._batch_norm_first,
    }
    base_config = super(BottleneckResidualInner, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))

  def call(self,
           inputs: tf.Tensor,
           training: Optional[bool] = None) -> tf.Tensor:
    x = inputs
    if self._batch_norm_first:
      x = self._batch_norm_0(x, training=training)
      x = self._activation_fn(x)
    x = self._conv2d_1(x)

    x = self._batch_norm_1(x, training=training)
    x = self._activation_fn(x)
    x = self._conv2d_2(x)

    x = self._batch_norm_2(x, training=training)
    x = self._activation_fn(x)
    x = self._conv2d_3(x)

    return x


@tf.keras.utils.register_keras_serializable(package='Vision')
class ReversibleLayer(tf.keras.layers.Layer):
  """Creates a reversible layer.

  Computes y1 = x1 + f(x2), y2 = x2 + g(y1), where f and g can be arbitrary
  layers that are stateless, which in this case are `ResidualInner` layers.
  """

  def __init__(self,
               f: tf.keras.layers.Layer,
               g: tf.keras.layers.Layer,
               manual_grads: bool = True,
               **kwargs):
    """Initializes a ReversibleLayer.

    Args:
      f: A `tf.keras.layers.Layer` instance of `f` inner block referred to in
        paper. Each reversible layer consists of two inner functions. For
        example, in RevNet the reversible residual consists of two f/g inner
        (bottleneck) residual functions. Where the input to the reversible layer
        is x, the input gets partitioned in the channel dimension and the
        forward pass follows (eq8): x = [x1; x2], z1 = x1 + f(x2), y2 = x2 +
        g(z1), y1 = stop_gradient(z1).
      g: A `tf.keras.layers.Layer` instance of `g` inner block referred to in
        paper. Detailed explanation same as above as `f` arg.
      manual_grads: A `bool` [Testing Only] of whether to manually take
        gradients as in Algorithm 1 or defer to autograd.
      **kwargs: Additional keyword arguments to be passed.
    """
    super(ReversibleLayer, self).__init__(**kwargs)

    self._f = f
    self._g = g
    self._manual_grads = manual_grads

    if tf.keras.backend.image_data_format() == 'channels_last':
      self._axis = -1
    else:
      self._axis = 1

  def get_config(self) -> Dict[str, Any]:
    config = {
        'f': self._f,
        'g': self._g,
        'manual_grads': self._manual_grads,
    }
    base_config = super(ReversibleLayer, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))

  def _ckpt_non_trainable_vars(self):
    self._f_non_trainable_vars = [
        v.read_value() for v in self._f.non_trainable_variables
    ]
    self._g_non_trainable_vars = [
        v.read_value() for v in self._g.non_trainable_variables
    ]

  def _load_ckpt_non_trainable_vars(self):
    for v, v_chkpt in zip(self._f.non_trainable_variables,
                          self._f_non_trainable_vars):
      v.assign(v_chkpt)
    for v, v_chkpt in zip(self._g.non_trainable_variables,
                          self._g_non_trainable_vars):
      v.assign(v_chkpt)

  def call(self,
           inputs: tf.Tensor,
           training: Optional[bool] = None) -> tf.Tensor:

    @tf.custom_gradient
    def reversible(
        x: tf.Tensor
    ) -> Tuple[tf.Tensor, Callable[[Any], Tuple[List[tf.Tensor],
                                                List[tf.Tensor]]]]:
      """Implements Algorithm 1 in the RevNet paper.

         Aidan N. Gomez, Mengye Ren, Raquel Urtasun, Roger B. Grosse.
         The Reversible Residual Network: Backpropagation Without Storing
         Activations.
         (https://arxiv.org/pdf/1707.04585.pdf)

      Args:
        x: An input `tf.Tensor.

      Returns:
        y: The output [y1; y2] in Algorithm 1.
        grad_fn: A callable function that computes the gradients.
      """
      with tf.GradientTape() as fwdtape:
        fwdtape.watch(x)
        x1, x2 = tf.split(x, num_or_size_splits=2, axis=self._axis)
        f_x2 = self._f(x2, training=training)
        x1_down = _maybe_downsample(x1, f_x2.shape[self._axis], self._f.strides,
                                    self._axis)
        z1 = f_x2 + x1_down
        g_z1 = self._g(z1, training=training)
        x2_down = _maybe_downsample(x2, g_z1.shape[self._axis], self._f.strides,
                                    self._axis)
        y2 = x2_down + g_z1

        # Equation 8: https://arxiv.org/pdf/1707.04585.pdf
        # Decouple y1 and z1 so that their derivatives are different.
        y1 = tf.identity(z1)
        y = tf.concat([y1, y2], axis=self._axis)

        irreversible = ((self._f.strides != 1 or self._g.strides != 1) or
                        (y.shape[self._axis] != inputs.shape[self._axis]))

        # Checkpointing moving mean/variance for batch normalization layers
        # as they shouldn't be updated during the custom gradient pass of f/g.
        self._ckpt_non_trainable_vars()

      def grad_fn(
          dy: tf.Tensor,
          variables: Optional[List[tf.Variable]] = None,
      ) -> Tuple[List[tf.Tensor], List[tf.Tensor]]:
        """Given dy calculate (dy/dx)|_{x_{input}} using f/g."""
        if irreversible or not self._manual_grads:
          grads_combined = fwdtape.gradient(
              y, [x] + variables, output_gradients=dy)
          dx = grads_combined[0]
          grad_vars = grads_combined[1:]
        else:
          y1_nograd = tf.stop_gradient(y1)
          y2_nograd = tf.stop_gradient(y2)
          dy1, dy2 = tf.split(dy, num_or_size_splits=2, axis=self._axis)

          # Index mapping from self.f/g.trainable_variables to grad_fn
          # input `variables` kwarg so that we can reorder dwf + dwg
          # variable gradient list to match `variables` order.
          f_var_refs = [v.ref() for v in self._f.trainable_variables]
          g_var_refs = [v.ref() for v in self._g.trainable_variables]
          fg_var_refs = f_var_refs + g_var_refs
          self_to_var_index = [fg_var_refs.index(v.ref()) for v in variables]

          # Algorithm 1 in paper (line # documented in-line)
          z1 = y1_nograd  # line 2
          with tf.GradientTape() as gtape:
            gtape.watch(z1)
            g_z1 = self._g(z1, training=training)
          x2 = y2_nograd - g_z1  # line 3

          with tf.GradientTape() as ftape:
            ftape.watch(x2)
            f_x2 = self._f(x2, training=training)
          x1 = z1 - f_x2  # pylint: disable=unused-variable      # line 4

          # Compute gradients
          g_grads_combined = gtape.gradient(
              g_z1, [z1] + self._g.trainable_variables, output_gradients=dy2)
          dz1 = dy1 + g_grads_combined[0]  # line 5
          dwg = g_grads_combined[1:]  # line 9

          f_grads_combined = ftape.gradient(
              f_x2, [x2] + self._f.trainable_variables, output_gradients=dz1)
          dx2 = dy2 + f_grads_combined[0]  # line 6
          dwf = f_grads_combined[1:]  # line 8
          dx1 = dz1  # line 7

          # Pack the input and variable gradients.
          dx = tf.concat([dx1, dx2], axis=self._axis)
          grad_vars = dwf + dwg
          # Reorder gradients (trainable_variables to variables kwarg order)
          grad_vars = [grad_vars[i] for i in self_to_var_index]

          # Restore batch normalization moving mean/variance for correctness.
          self._load_ckpt_non_trainable_vars()

        return dx, grad_vars  # grad_fn end

      return y, grad_fn  # reversible end

    activations = reversible(inputs)
    return activations


@tf.keras.utils.register_keras_serializable(package='Vision')
class DepthwiseSeparableConvBlock(tf.keras.layers.Layer):
  """Creates a depthwise separable convolution block with batch normalization.
  """

  def __init__(
      self,
      filters: int,
      kernel_size: int = 3,
      strides: int = 1,
      regularize_depthwise=False,
      activation: Text = 'relu6',
      kernel_initializer: Text = 'VarianceScaling',
      kernel_regularizer: Optional[tf.keras.regularizers.Regularizer] = None,
      dilation_rate: int = 1,
      use_sync_bn: bool = False,
      norm_momentum: float = 0.99,
      norm_epsilon: float = 0.001,
      **kwargs):
    """Initializes a convolution block with batch normalization.

    Args:
      filters: An `int` number of filters for the first two convolutions. Note
        that the third and final convolution will use 4 times as many filters.
      kernel_size: An `int` that specifies the height and width of the 2D
        convolution window.
      strides: An `int` of block stride. If greater than 1, this block will
        ultimately downsample the input.
      regularize_depthwise: A `bool`. If Ture, apply regularization on
        depthwise.
      activation: A `str` name of the activation function.
      kernel_initializer: A `str` of kernel_initializer for convolutional
        layers.
      kernel_regularizer: A `tf.keras.regularizers.Regularizer` object for
        Conv2D. Default to None.
      dilation_rate: An `int` or tuple/list of 2 `int`, specifying the dilation
        rate to use for dilated convolution. Can be a single integer to specify
        the same value for all spatial dimensions.
      use_sync_bn: A `bool`. If True, use synchronized batch normalization.
      norm_momentum: A `float` of normalization momentum for the moving average.
      norm_epsilon: A `float` added to variance to avoid dividing by zero.
      **kwargs: Additional keyword arguments to be passed.
    """
    super(DepthwiseSeparableConvBlock, self).__init__(**kwargs)
    self._filters = filters
    self._kernel_size = kernel_size
    self._strides = strides
    self._activation = activation
    self._regularize_depthwise = regularize_depthwise
    self._kernel_initializer = kernel_initializer
    self._kernel_regularizer = kernel_regularizer
    self._dilation_rate = dilation_rate
    self._use_sync_bn = use_sync_bn
    self._norm_momentum = norm_momentum
    self._norm_epsilon = norm_epsilon
    self._norm = tf.keras.layers.BatchNormalization

    if tf.keras.backend.image_data_format() == 'channels_last':
      self._bn_axis = -1
    else:
      self._bn_axis = 1
    self._activation_fn = tf_utils.get_activation(activation)
    if regularize_depthwise:
      self._depthsize_regularizer = kernel_regularizer
    else:
      self._depthsize_regularizer = None

  def get_config(self):
    config = {
        'filters': self._filters,
        'strides': self._strides,
        'regularize_depthwise': self._regularize_depthwise,
        'kernel_initializer': self._kernel_initializer,
        'kernel_regularizer': self._kernel_regularizer,
        'activation': self._activation,
        'use_sync_bn': self._use_sync_bn,
        'norm_momentum': self._norm_momentum,
        'norm_epsilon': self._norm_epsilon
    }
    base_config = super(DepthwiseSeparableConvBlock, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))

  def build(self, input_shape):

    self._dwconv0 = tf.keras.layers.DepthwiseConv2D(
        kernel_size=self._kernel_size,
        strides=self._strides,
        padding='same',
        depth_multiplier=1,
        dilation_rate=self._dilation_rate,
        kernel_initializer=tf_utils.clone_initializer(self._kernel_initializer),
        kernel_regularizer=self._depthsize_regularizer,
        use_bias=False)
    self._norm0 = self._norm(
        axis=self._bn_axis,
        momentum=self._norm_momentum,
        epsilon=self._norm_epsilon,
        synchronized=self._use_sync_bn,
    )

    self._conv1 = tf.keras.layers.Conv2D(
        filters=self._filters,
        kernel_size=1,
        strides=1,
        padding='same',
        use_bias=False,
        kernel_initializer=tf_utils.clone_initializer(self._kernel_initializer),
        kernel_regularizer=self._kernel_regularizer)
    self._norm1 = self._norm(
        axis=self._bn_axis,
        momentum=self._norm_momentum,
        epsilon=self._norm_epsilon,
        synchronized=self._use_sync_bn,
    )

    super(DepthwiseSeparableConvBlock, self).build(input_shape)

  def call(self, inputs, training=None):
    x = self._dwconv0(inputs)
    x = self._norm0(x)
    x = self._activation_fn(x)

    x = self._conv1(x)
    x = self._norm1(x)
    return self._activation_fn(x)


@tf.keras.utils.register_keras_serializable(package='Vision')
class TuckerConvBlock(tf.keras.layers.Layer):
  """An Tucker block (generalized bottleneck)."""

  def __init__(self,
               in_filters,
               out_filters,
               input_compression_ratio,
               output_compression_ratio,
               strides,
               kernel_size=3,
               stochastic_depth_drop_rate=None,
               kernel_initializer='VarianceScaling',
               kernel_regularizer=None,
               bias_regularizer=None,
               activation='relu',
               use_sync_bn=False,
               divisible_by=1,
               use_residual=True,
               norm_momentum=0.99,
               norm_epsilon=0.001,
               **kwargs):
    """Initializes an inverted bottleneck block with BN after convolutions.

    Args:
      in_filters: An `int` number of filters of the input tensor.
      out_filters: An `int` number of filters of the output tensor.
      input_compression_ratio: An `float` of compression ratio for input
        filters.
      output_compression_ratio: An `float` of compression ratio for output
        filters.
      strides: An `int` block stride. If greater than 1, this block will
        ultimately downsample the input.
      kernel_size: An `int` kernel_size of the depthwise conv layer.
      stochastic_depth_drop_rate: A `float` or None. if not None, drop rate for
        the stochastic depth layer.
      kernel_initializer: A `str` of kernel_initializer for convolutional
        layers.
      kernel_regularizer: A `tf.keras.regularizers.Regularizer` object for
        Conv2D. Default to None.
      bias_regularizer: A `tf.keras.regularizers.Regularizer` object for Conv2d.
        Default to None.
      activation: A `str` name of the activation function.
      use_sync_bn: A `bool`. If True, use synchronized batch normalization.
      divisible_by: An `int` that ensures all inner dimensions are divisible by
        this number.
      use_residual: A `bool` of whether to include residual connection between
        input and output.
      norm_momentum: A `float` of normalization momentum for the moving average.
      norm_epsilon: A `float` added to variance to avoid dividing by zero.
      **kwargs: Additional keyword arguments to be passed.
    """
    super(TuckerConvBlock, self).__init__(**kwargs)

    self._in_filters = in_filters
    self._out_filters = out_filters
    self._input_compression_ratio = input_compression_ratio
    self._output_compression_ratio = output_compression_ratio
    self._strides = strides
    self._kernel_size = kernel_size
    self._divisible_by = divisible_by
    self._stochastic_depth_drop_rate = stochastic_depth_drop_rate
    self._use_sync_bn = use_sync_bn
    self._use_residual = use_residual
    self._activation = activation
    self._kernel_initializer = kernel_initializer
    self._norm_momentum = norm_momentum
    self._norm_epsilon = norm_epsilon
    self._kernel_regularizer = kernel_regularizer
    self._bias_regularizer = bias_regularizer
    self._norm = tf.keras.layers.BatchNormalization

    if tf.keras.backend.image_data_format() == 'channels_last':
      self._bn_axis = -1
    else:
      self._bn_axis = 1

  def build(self, input_shape):
    input_compressed_filters = nn_layers.make_divisible(
        value=self._in_filters * self._input_compression_ratio,
        divisor=self._divisible_by,
        round_down_protect=False)

    self._conv0 = tf.keras.layers.Conv2D(
        filters=input_compressed_filters,
        kernel_size=1,
        strides=1,
        padding='same',
        use_bias=False,
        kernel_initializer=tf_utils.clone_initializer(self._kernel_initializer),
        kernel_regularizer=self._kernel_regularizer,
        bias_regularizer=self._bias_regularizer)
    self._norm0 = self._norm(
        axis=self._bn_axis,
        momentum=self._norm_momentum,
        epsilon=self._norm_epsilon,
        synchronized=self._use_sync_bn,
    )
    self._activation_layer0 = tf_utils.get_activation(
        self._activation, use_keras_layer=True)

    output_compressed_filters = nn_layers.make_divisible(
        value=self._out_filters * self._output_compression_ratio,
        divisor=self._divisible_by,
        round_down_protect=False)

    self._conv1 = tf.keras.layers.Conv2D(
        filters=output_compressed_filters,
        kernel_size=self._kernel_size,
        strides=self._strides,
        padding='same',
        use_bias=False,
        kernel_initializer=tf_utils.clone_initializer(self._kernel_initializer),
        kernel_regularizer=self._kernel_regularizer,
        bias_regularizer=self._bias_regularizer)
    self._norm1 = self._norm(
        axis=self._bn_axis,
        momentum=self._norm_momentum,
        epsilon=self._norm_epsilon,
        synchronized=self._use_sync_bn,
    )
    self._activation_layer1 = tf_utils.get_activation(
        self._activation, use_keras_layer=True)

    # Last 1x1 conv.
    self._conv2 = tf.keras.layers.Conv2D(
        filters=self._out_filters,
        kernel_size=1,
        strides=1,
        padding='same',
        use_bias=False,
        kernel_initializer=tf_utils.clone_initializer(self._kernel_initializer),
        kernel_regularizer=self._kernel_regularizer,
        bias_regularizer=self._bias_regularizer)
    self._norm2 = self._norm(
        axis=self._bn_axis,
        momentum=self._norm_momentum,
        epsilon=self._norm_epsilon,
        synchronized=self._use_sync_bn,
    )

    if self._stochastic_depth_drop_rate:
      self._stochastic_depth = nn_layers.StochasticDepth(
          self._stochastic_depth_drop_rate)
    else:
      self._stochastic_depth = None
    self._add = tf.keras.layers.Add()

    super(TuckerConvBlock, self).build(input_shape)

  def get_config(self):
    config = {
        'in_filters': self._in_filters,
        'out_filters': self._out_filters,
        'input_compression_ratio': self._input_compression_ratio,
        'output_compression_ratio': self._output_compression_ratio,
        'strides': self._strides,
        'kernel_size': self._kernel_size,
        'divisible_by': self._divisible_by,
        'stochastic_depth_drop_rate': self._stochastic_depth_drop_rate,
        'kernel_initializer': self._kernel_initializer,
        'kernel_regularizer': self._kernel_regularizer,
        'bias_regularizer': self._bias_regularizer,
        'activation': self._activation,
        'use_sync_bn': self._use_sync_bn,
        'use_residual': self._use_residual,
        'norm_momentum': self._norm_momentum,
        'norm_epsilon': self._norm_epsilon
    }
    base_config = super(TuckerConvBlock, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))

  def call(self, inputs, training=None):
    shortcut = inputs

    x = self._conv0(inputs)
    x = self._norm0(x)
    x = self._activation_layer0(x)

    x = self._conv1(x)
    x = self._norm1(x)
    x = self._activation_layer1(x)

    x = self._conv2(x)
    x = self._norm2(x)

    if (self._use_residual and self._in_filters == self._out_filters and
        self._strides == 1):
      if self._stochastic_depth:
        x = self._stochastic_depth(x, training=training)
      x = self._add([x, shortcut])

    return x


@tf.keras.utils.register_keras_serializable(package='Vision')
class LayerScale(tf.keras.layers.Layer):
  """LayerScale as introduced in CaiT: https://arxiv.org/abs/2103.17239.

  Attributes:
      init_values (float): value to initialize the diagonal matrix of
        LayerScale.
  """

  def __init__(self, init_values: float, **kwargs):
    """Initializes LayerScale."""
    super().__init__(**kwargs)
    self.gamma_init_value = init_values

  def build(self, inputs_shape):
    gamma_shape = (1, 1, inputs_shape[2])
    self.gamma = self.add_weight(
        name='layerscale_gamma',
        shape=gamma_shape,
        initializer=tf.keras.initializers.Constant(self.gamma_init_value),
        trainable=True,
        dtype=tf.float32,
    )

  def call(self, inputs, inputs_positions=None):
    del inputs_positions
    input_dtype = inputs.dtype
    gamma = self.gamma
    return tf.cast(tf.cast(inputs, tf.float32) * gamma, input_dtype)


@tf.keras.utils.register_keras_serializable(package='Vision')
class TransformerEncoderBlock(nlp_modeling.layers.TransformerEncoderBlock):
  """TransformerEncoderBlock layer with stochastic depth and layerscale."""

  def __init__(
      self,
      *args,
      stochastic_depth_drop_rate=0.0,
      layer_scale_init_value=0.0,
      max_attention_inference_parallelism=None,
      **kwargs
  ):
    """Initializes TransformerEncoderBlock.

    Args:
      *args: positional arguments passed to super().__init__.
      stochastic_depth_drop_rate: the drop rate for the stochastic depth layer.
      layer_scale_init_value:
      max_attention_inference_parallelism: the number of examples to run in
        parallel in the attention blocks during inference. Set this limit to
        reduce the peak memory usage. If None, use vectorized operations to run
        the whole batch in parallel.
      **kwargs: keyword arguments passed to super().__init__.
    """
    super().__init__(*args, **kwargs)
    self._stochastic_depth_drop_rate = stochastic_depth_drop_rate
    self._layer_scale_init_value = layer_scale_init_value
    self._max_attention_inference_parallelism = (
        max_attention_inference_parallelism
    )

  def build(self, input_shape):
    if self._stochastic_depth_drop_rate:
      self._stochastic_depth = nn_layers.StochasticDepth(
          self._stochastic_depth_drop_rate)
    else:
      self._stochastic_depth = lambda x, *args, **kwargs: tf.identity(x)

    if self._layer_scale_init_value:
      self._layer_scale_attn = LayerScale(
          init_values=self._layer_scale_init_value, name='layer_scale_attn')
      self._layer_scale_mlp = LayerScale(
          init_values=self._layer_scale_init_value, name='layer_scale_mlp')
    else:
      self._layer_scale_attn = lambda x, *args, **kwargs: tf.identity(x)
      self._layer_scale_mlp = lambda x, *args, **kwargs: tf.identity(x)
    super().build(input_shape)

    if self._max_attention_inference_parallelism is not None:
      attention_layer_config = self._attention_layer.get_config()
      self._attention_layer = nn_layers.MultiHeadAttention.from_config({
          **attention_layer_config,
          'max_inference_parallelism': (
              self._max_attention_inference_parallelism
          ),
      })

  def get_config(self):
    config = super().get_config()
    config.update({
        'stochastic_depth_drop_rate': self._stochastic_depth_drop_rate,
        'layer_scale_init_value': self._layer_scale_init_value,
        'max_attention_inference_parallelism': (
            self._max_attention_inference_parallelism
        ),
    })
    return config

  def call(self, inputs, output_range=None, training=None):
    """Transformer self-attention encoder block call."""
    if isinstance(inputs, (list, tuple)):
      if len(inputs) == 2:
        input_tensor, attention_mask = inputs
        key_value = None
      elif len(inputs) == 3:
        input_tensor, key_value, attention_mask = inputs
      else:
        raise ValueError('Unexpected inputs to %s with length at %d' %
                         (self.__class__, len(inputs)))
    else:
      input_tensor, key_value, attention_mask = (inputs, None, None)

    if output_range is None:
      output_range = self._output_range
    if output_range:
      if self._norm_first:
        source_tensor = input_tensor[:, 0:output_range, :]
        input_tensor = self._attention_layer_norm(input_tensor)
        if key_value is not None:
          key_value = self._attention_layer_norm(key_value)
      target_tensor = input_tensor[:, 0:output_range, :]
      if attention_mask is not None:
        attention_mask = attention_mask[:, 0:output_range, :]
    else:
      if self._norm_first:
        source_tensor = input_tensor
        input_tensor = self._attention_layer_norm(input_tensor)
        if key_value is not None:
          key_value = self._attention_layer_norm(key_value)
      target_tensor = input_tensor

    if key_value is None:
      key_value = input_tensor

    attention_output, attention_scores = self._attention_layer(
        query=target_tensor,
        value=key_value,
        attention_mask=attention_mask,
        return_attention_scores=True)
    attention_output = self._attention_dropout(attention_output)

    attention_output = self._layer_scale_attn(attention_output)

    if self._norm_first:
      # Important to not combine `self._norm_first` and
      # `self._use_query_residual` into one if clause because else is only for
      # `_norm_first == False`.
      if self._use_query_residual:
        attention_output = source_tensor + self._stochastic_depth(
            attention_output, training=training)
      source_attention_output = attention_output
      attention_output = self._output_layer_norm(attention_output)
    else:
      if self._use_query_residual:
        attention_output = target_tensor + self._stochastic_depth(
            attention_output, training=training)
      attention_output = self._attention_layer_norm(attention_output)

    inner_output = self._intermediate_dense(attention_output)
    inner_output = self._intermediate_activation_layer(inner_output)
    inner_output = self._inner_dropout_layer(inner_output)
    layer_output = self._output_dense(inner_output)
    layer_output = self._output_dropout(layer_output)

    # Layerscale after MLP.
    layer_output = self._layer_scale_mlp(layer_output)

    if self._norm_first:
      layer_output = source_attention_output + self._stochastic_depth(
          layer_output, training=training)
    else:
      # During mixed precision training, layer norm output is always fp32 for
      # now. Casts fp32 for the subsequent add.
      layer_output = tf.cast(layer_output, tf.float32)
      layer_output = self._output_layer_norm(
          layer_output
          + self._stochastic_depth(attention_output, training=training))

    if self._return_attention_scores:
      return layer_output, attention_scores
    else:
      return layer_output


@tf.keras.utils.register_keras_serializable(package='Vision')
class TransformerScaffold(nlp_modeling.layers.TransformerScaffold):
  """TransformerScaffold layer for vision applications."""

  def __init__(
      self,
      *args,
      stochastic_depth_drop_rate: float = 0.0,
      return_attention_scores: bool = False,
      ffn_has_residual_connection: bool = False,
      max_attention_inference_parallelism: Optional[int] = None,
      **kwargs
  ):
    """Initializes TransformerEncoderBlock.

    Args:
      *args: positional arguments passed to super().__init__.
      stochastic_depth_drop_rate: the drop rate for the stochastic depth layer.
      return_attention_scores: whether to return the attention output.
      ffn_has_residual_connection: whether the feedforward network has internal
        residual connection and layer norm. If False, the residual connection
        and the layer norm op are called inside TransformerScaffold.
      max_attention_inference_parallelism: the number of examples to run in
        parallel in the attention blocks during inference. Set this limit to
        reduce the peak memory usage. If None, use vectorized operations to run
        the whole batch in parallel.
      **kwargs: keyword arguments passed to super().__init__.
    """
    super().__init__(*args, **kwargs)
    self._stochastic_depth_drop_rate = stochastic_depth_drop_rate
    self._return_attention_scores = return_attention_scores
    self._ffn_has_residual_connection = ffn_has_residual_connection
    self._max_attention_inference_parallelism = (
        max_attention_inference_parallelism
    )

  def build(self, input_shape: Union[tf.TensorShape, List[int]]):
    if self._stochastic_depth_drop_rate:
      self._stochastic_depth = nn_layers.StochasticDepth(
          self._stochastic_depth_drop_rate)
    else:
      self._stochastic_depth = lambda x, *args, **kwargs: tf.identity(x)

    super().build(input_shape)

    if self._max_attention_inference_parallelism is not None:
      attention_layer_config = self._attention_layer.get_config()
      self._attention_layer = self._attention_cls.from_config({
          **attention_layer_config,
          'max_inference_parallelism': (
              self._max_attention_inference_parallelism
          ),
      })

  def get_config(self):
    config = super().get_config()
    config.update({
        'stochastic_depth_drop_rate': self._stochastic_depth_drop_rate,
        'return_attention_scores': self._return_attention_scores,
        'ffn_has_residual_connection': self._ffn_has_residual_connection,
        'max_attention_inference_parallelism': (
            self._max_attention_inference_parallelism
        ),
    })
    return config

  def call(
      self,
      inputs: tf.Tensor,
      training: Optional[bool] = None
  ) -> Union[tf.Tensor, Tuple[tf.Tensor, tf.Tensor]]:
    """Transformer self-attention encoder block call."""
    if isinstance(inputs, (list, tuple)):
      if len(inputs) == 2:
        input_tensor, attention_mask = inputs
        key_value = None
      elif len(inputs) == 3:
        input_tensor, key_value, attention_mask = inputs
      else:
        raise ValueError('Unexpected inputs to %s with length at %d' %
                         (self.__class__, len(inputs)))
    else:
      input_tensor, key_value, attention_mask = (inputs, None, None)

    if self._norm_first:
      source_tensor = input_tensor
      input_tensor = self._attention_layer_norm(input_tensor)

    if key_value is None:
      key_value = input_tensor

    attention_output, attention_scores = self._attention_layer(
        query=input_tensor,
        value=key_value,
        attention_mask=attention_mask,
        training=training,
        return_attention_scores=True)
    attention_output = self._attention_dropout(
        attention_output, training=training)

    if self._norm_first:
      source_attention_output = source_tensor + self._stochastic_depth(
          attention_output, training=training)
      attention_output = self._output_layer_norm(
          source_attention_output)
    else:
      attention_output = self._attention_layer_norm(
          input_tensor +
          self._stochastic_depth(attention_output, training=training))

    if self._feedforward_block is None:
      intermediate_output = self._intermediate_dense(attention_output)
      intermediate_output = self._intermediate_activation_layer(
          intermediate_output)
      layer_output = self._output_dense(intermediate_output)
      layer_output = self._output_dropout(layer_output, training=training)
    else:
      layer_output = self._feedforward_block(
          attention_output, training=training)

    if self._norm_first:
      if self._ffn_has_residual_connection:
        raise ValueError(
            'In the case of `norm_first`, the residual connection should be'
            "done in the TransformerScaffold call function, not FFN's"
            'call function.')
      output = source_attention_output + self._stochastic_depth(
          layer_output, training=training)
    else:
      # During mixed precision training, layer norm output is always fp32 for
      # now. Casts fp32 for the subsequent add.
      layer_output = tf.cast(layer_output, tf.float32)
      if self._ffn_has_residual_connection:
        output = self._stochastic_depth(layer_output, training=training)
      else:
        output = self._output_layer_norm(
            attention_output +
            self._stochastic_depth(layer_output, training=training))

    if self._return_attention_scores:
      return output, attention_scores
    else:
      return output
