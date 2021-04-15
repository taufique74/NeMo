# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
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


from typing import Callable, List, Optional, Tuple

import torch
import torch.nn as nn
from torch import Tensor

from nemo.collections.asr.parts.activations import Swish

try:
    from pytorch_quantization import calib
    from pytorch_quantization import nn as quant_nn
    from pytorch_quantization import quant_modules
    from pytorch_quantization.tensor_quant import QuantDescriptor

    PYTORCH_QUANTIZATION_AVAILABLE = True
except ImportError:
    PYTORCH_QUANTIZATION_AVAILABLE = False


jasper_activations = {"hardtanh": nn.Hardtanh, "relu": nn.ReLU, "selu": nn.SELU, "swish": Swish}


def init_weights(m, mode: Optional[str] = 'xavier_uniform'):
    if isinstance(m, MaskedConv1d):
        init_weights(m.conv, mode)
    if isinstance(m, (nn.Conv1d, nn.Linear)):
        if mode is not None:
            if mode == 'xavier_uniform':
                nn.init.xavier_uniform_(m.weight, gain=1.0)
            elif mode == 'xavier_normal':
                nn.init.xavier_normal_(m.weight, gain=1.0)
            elif mode == 'kaiming_uniform':
                nn.init.kaiming_uniform_(m.weight, nonlinearity="relu")
            elif mode == 'kaiming_normal':
                nn.init.kaiming_normal_(m.weight, nonlinearity="relu")
            else:
                raise ValueError("Unknown Initialization mode: {0}".format(mode))
    elif isinstance(m, nn.BatchNorm1d):
        if m.track_running_stats:
            m.running_mean.zero_()
            m.running_var.fill_(1)
            m.num_batches_tracked.zero_()
        if m.affine:
            nn.init.ones_(m.weight)
            nn.init.zeros_(m.bias)


def compute_new_kernel_size(kernel_size, kernel_width):
    new_kernel_size = max(int(kernel_size * kernel_width), 1)
    # If kernel is even shape, round up to make it odd
    if new_kernel_size % 2 == 0:
        new_kernel_size += 1
    return new_kernel_size


def get_same_padding(kernel_size, stride, dilation):
    if stride > 1 and dilation > 1:
        raise ValueError("Only stride OR dilation may be greater than 1")
    return (dilation * (kernel_size - 1)) // 2


class StatsPoolLayer(nn.Module):
    def __init__(self, feat_in, pool_mode='xvector'):
        super().__init__()
        self.pool_mode = pool_mode
        self.feat_in = feat_in
        if self.pool_mode == 'xvector':
            self.feat_in += feat_in

    def forward(self, x):
        mean = x.mean(dim=-1)  # Time Axis
        if self.pool_mode == 'xvector':
            std = x.std(dim=-1)
            pooled = torch.cat([mean, std], dim=-1)
        else:
            pooled = mean
        return pooled


class AttentivePoolingLayer(nn.Module):
    def __init__(self,input_channels,attention_channels,global_context=True,init_mode='xavier_uniform'):
        super().__init__()
        self.global_context = global_context
        self.feat_in = 2*input_channels
        if self.global_context:
            self.channel_attn = nn.Conv1d(3*input_channels,attention_channels,kernel_size=1,dilation=1)
        else:
            self.channel_attn = nn.Conv1d(input_channels,attention_channels,kernel_size=1,dilation=1)
        
        self.linear_activation = nn.Tanh()
        self.channel_activation = nn.ReLU()
        self.norm = nn.BatchNorm1d(attention_channels)
        self.scalar_attn = nn.Conv1d(attention_channels,input_channels,kernel_size=1,dilation=1)

        self.apply(lambda x: init_weights(x, mode=init_mode))
    
    def compute_stats(self,x,m,dim=2):
        eps = 1e-12
        mean = (m * x).sum(dim)
        std = torch.sqrt(
                (m * (x - mean.unsqueeze(dim)).pow(2)).sum(dim).clamp(eps)
            )
        return mean, std
    
    def forward(self,x,lengths=None):

        T = x.shape[-1]
        if lengths is None:
            lengths = torch.ones(x.shape[0],device=x.device)
        
        mask = torch.arange(T,device=x.device).expand(len(lengths), T) < lengths.unsqueeze(1) # Nx1
        mask = torch.as_tensor(mask).unsqueeze(1) # Nx1xT

        if self.global_context:
            total = mask.sum(dim=2, keepdim=True).float()
            mean, std = self.compute_stats(x, mask / total)
            mean = mean.unsqueeze(2).repeat(1, 1, T)
            std = std.unsqueeze(2).repeat(1, 1, T)
            attn = torch.cat([x, mean, std], dim=1)
        else:
            attn = x
        
        scaler_score = self.norm(self.channel_activation(self.channel_attn(attn)))
        
        attn = self.scalar_attn(self.linear_activation(scaler_score))

        # Filter out zero-paddings
        attn = attn.masked_fill(mask == 0, float("-inf"))

        attn = nn.functional.softmax(attn, dim=2)
        mean, std = self.compute_stats(x, attn)
        # Append mean and std of the batch
        pooled_stats = torch.cat((mean, std), dim=1)

        return pooled_stats


class MaskedConv1d(nn.Module):
    __constants__ = ["use_conv_mask", "real_out_channels", "heads"]

    def __init__(
        self,
        in_channels,
        out_channels,
        kernel_size,
        stride=1,
        padding=0,
        dilation=1,
        groups=1,
        heads=-1,
        bias=False,
        use_mask=True,
        quantize=False,
    ):
        super(MaskedConv1d, self).__init__()

        if not (heads == -1 or groups == in_channels):
            raise ValueError("Only use heads for depthwise convolutions")

        self.real_out_channels = out_channels
        if heads != -1:
            in_channels = heads
            out_channels = heads
            groups = heads

        if PYTORCH_QUANTIZATION_AVAILABLE and quantize:
            self.conv = quant_nn.QuantConv1d(
                in_channels,
                out_channels,
                kernel_size,
                stride=stride,
                padding=padding,
                dilation=dilation,
                groups=groups,
                bias=bias,
            )
        elif not PYTORCH_QUANTIZATION_AVAILABLE and quantize:
            raise ImportError(
                "pytorch-quantization is not installed. Install from "
                "https://github.com/NVIDIA/TensorRT/tree/master/tools/pytorch-quantization."
            )
        else:
            self.conv = nn.Conv1d(
                in_channels,
                out_channels,
                kernel_size,
                stride=stride,
                padding=padding,
                dilation=dilation,
                groups=groups,
                bias=bias,
            )
        self.use_mask = use_mask
        self.heads = heads

    def get_seq_len(self, lens):
        return (
            lens + 2 * self.conv.padding[0] - self.conv.dilation[0] * (self.conv.kernel_size[0] - 1) - 1
        ) // self.conv.stride[0] + 1

    def forward(self, x, lens):
        if self.use_mask:
            lens = lens.to(dtype=torch.long)
            max_len = x.size(2)
            mask = torch.arange(max_len).to(lens.device).expand(len(lens), max_len) >= lens.unsqueeze(1)
            x = x.masked_fill(mask.unsqueeze(1).to(device=x.device), 0)
            # del mask
            lens = self.get_seq_len(lens)

        sh = x.shape
        if self.heads != -1:
            x = x.view(-1, self.heads, sh[-1])

        out = self.conv(x)

        if self.heads != -1:
            out = out.view(sh[0], self.real_out_channels, -1)

        return out, lens


class GroupShuffle(nn.Module):
    def __init__(self, groups, channels):
        super(GroupShuffle, self).__init__()

        self.groups = groups
        self.channels_per_group = channels // groups

    def forward(self, x):
        sh = x.shape

        x = x.view(-1, self.groups, self.channels_per_group, sh[-1])

        x = torch.transpose(x, 1, 2).contiguous()

        x = x.view(-1, self.groups * self.channels_per_group, sh[-1])

        return x


class SqueezeExcite(nn.Module):
    def __init__(
        self,
        channels: int,
        reduction_ratio: int,
        context_window: int = -1,
        interpolation_mode: str = 'nearest',
        activation: Optional[Callable] = None,
        quantize: bool = False,
    ):
        """
        Squeeze-and-Excitation sub-module.

        Args:
            channels: Input number of channels.
            reduction_ratio: Reduction ratio for "squeeze" layer.
            context_window: Integer number of timesteps that the context
                should be computed over, using stride 1 average pooling.
                If value < 1, then global context is computed.
            interpolation_mode: Interpolation mode of timestep dimension.
                Used only if context window is > 1.
                The modes available for resizing are: `nearest`, `linear` (3D-only),
                `bilinear`, `area`
            activation: Intermediate activation function used. Must be a
                callable activation function.
        """
        super(SqueezeExcite, self).__init__()
        self.context_window = int(context_window)
        self.interpolation_mode = interpolation_mode

        if self.context_window <= 0:
            if PYTORCH_QUANTIZATION_AVAILABLE and quantize:
                self.pool = quant_nn.QuantAdaptiveAvgPool1d(1)  # context window = T
            elif not PYTORCH_QUANTIZATION_AVAILABLE and quantize:
                raise ImportError(
                    "pytorch-quantization is not installed. Install from "
                    "https://github.com/NVIDIA/TensorRT/tree/master/tools/pytorch-quantization."
                )
            else:
                self.pool = nn.AdaptiveAvgPool1d(1)  # context window = T
        else:
            if PYTORCH_QUANTIZATION_AVAILABLE and quantize:
                self.pool = quant_nn.QuantAvgPool1d(self.context_window, stride=1)
            elif not PYTORCH_QUANTIZATION_AVAILABLE and quantize:
                raise ImportError(
                    "pytorch-quantization is not installed. Install from "
                    "https://github.com/NVIDIA/TensorRT/tree/master/tools/pytorch-quantization."
                )
            else:
                self.pool = nn.AvgPool1d(self.context_window, stride=1)

        if activation is None:
            activation = nn.ReLU(inplace=True)

        if PYTORCH_QUANTIZATION_AVAILABLE and quantize:
            self.fc = nn.Sequential(
                quant_nn.QuantLinear(channels, channels // reduction_ratio, bias=False),
                activation,
                quant_nn.QuantLinear(channels // reduction_ratio, channels, bias=False),
            )
        elif not PYTORCH_QUANTIZATION_AVAILABLE and quantize:
            raise ImportError(
                "pytorch-quantization is not installed. Install from "
                "https://github.com/NVIDIA/TensorRT/tree/master/tools/pytorch-quantization."
            )
        else:
            self.fc = nn.Sequential(
                nn.Linear(channels, channels // reduction_ratio, bias=False),
                activation,
                nn.Linear(channels // reduction_ratio, channels, bias=False),
            )

    def forward(self, x):
        # The use of negative indices on the transpose allow for expanded SqueezeExcite
        batch, channels, timesteps = x.size()[:3]
        y = self.pool(x)  # [B, C, T - context_window + 1]
        y = y.transpose(1, -1)  # [B, T - context_window + 1, C]
        y = self.fc(y)  # [B, T - context_window + 1, C]
        y = y.transpose(1, -1)  # [B, C, T - context_window + 1]

        if self.context_window > 0:
            y = torch.nn.functional.interpolate(y, size=timesteps, mode=self.interpolation_mode)

        y = torch.sigmoid(y)

        return x * y


class JasperBlock(nn.Module):
    """
    Constructs a single "Jasper" block. With modified parameters, also constructs other blocks for models
    such as `QuartzNet` and `Citrinet`.

    - For `Jasper`    : `separable` flag should be False
    - For `QuartzNet` : `separable` flag should be True
    - For `Citrinet`  : `separable` flag and `se` flag should be True

    Note that above are general distinctions, each model has intricate differences that expand over
    multiple such blocks.

    For further information about the differences between models which use JasperBlock, please review
    the configs for ASR models found in the ASR examples directory.

    Args:
        inplanes: Number of input channels.
        planes: Number of output channels.
        repeat: Number of repeated sub-blocks (R) for this block.
        kernel_size: Convolution kernel size across all repeated sub-blocks.
        kernel_size_factor: Floating point scale value that is multiplied with kernel size,
            then rounded down to nearest odd integer to compose the kernel size. Defaults to 1.0.
        stride: Stride of the convolutional layers.
        dilation: Integer which defined dilation factor of kernel. Note that when dilation > 1, stride must
            be equal to 1.
        padding: String representing type of padding. Currently only supports "same" padding,
            which symmetrically pads the input tensor with zeros.
        dropout: Floating point value, determins percentage of output that is zeroed out.
        activation: String representing activation functions. Valid activation functions are :
            {"hardtanh": nn.Hardtanh, "relu": nn.ReLU, "selu": nn.SELU, "swish": Swish}.
            Defaults to "relu".
        residual: Bool that determined whether a residual branch should be added or not.
            All residual branches are constructed using a pointwise convolution kernel, that may or may not
            perform strided convolution depending on the parameter `residual_mode`.
        groups: Number of groups for Grouped Convolutions. Defaults to 1.
        separable: Bool flag that describes whether Time-Channel depthwise separable convolution should be
            constructed, or ordinary convolution should be constructed.
        heads: Number of "heads" for the masked convolution. Defaults to -1, which disables it.
        normalization: String that represents type of normalization performed. Can be one of
            "batch", "group", "instance" or "layer" to compute BatchNorm1D, GroupNorm1D, InstanceNorm or
            LayerNorm (which are special cases of GroupNorm1D).
        norm_groups: Number of groups used for GroupNorm (if `normalization` == "group").
        residual_mode: String argument which describes whether the residual branch should be simply
            added ("add") or should first stride, then add ("stride_add"). Required when performing stride on
            parallel branch as well as utilizing residual add.
        residual_panes: Number of residual panes, used for Jasper-DR models. Please refer to the paper.
        conv_mask: Bool flag which determines whether to utilize masked convolutions or not. In general,
            it should be set to True.
        se: Bool flag that determines whether Squeeze-and-Excitation layer should be used.
        se_reduction_ratio: Integer value, which determines to what extend the hidden dimension of the SE
            intermediate step should be reduced. Larger values reduce number of parameters, but also limit
            the effectiveness of SE layers.
        se_context_window: Integer value determining the number of timesteps that should be utilized in order
            to compute the averaged context window. Defaults to -1, which means it uses global context - such
            that all timesteps are averaged. If any positive integer is used, it will utilize limited context
            window of that size.
        se_interpolation_mode: String used for interpolation mode of timestep dimension for SE blocks.
            Used only if context window is > 1.
            The modes available for resizing are: `nearest`, `linear` (3D-only),
            `bilinear`, `area`.
        stride_last: Bool flag that determines whether all repeated blocks should stride at once,
            (stride of S^R when this flag is False) or just the last repeated block should stride
            (stride of S when this flag is True).
        quantize: Bool flag whether to quantize the Convolutional blocks.
    """

    __constants__ = ["conv_mask", "separable", "residual_mode", "res", "mconv"]

    def __init__(
        self,
        inplanes,
        planes,
        repeat=3,
        kernel_size=11,
        kernel_size_factor=1,
        stride=1,
        dilation=1,
        padding='same',
        dropout=0.2,
        activation=None,
        residual=True,
        groups=1,
        separable=False,
        heads=-1,
        normalization="batch",
        norm_groups=1,
        residual_mode='add',
        residual_panes=[],
        conv_mask=False,
        se=False,
        se_reduction_ratio=16,
        se_context_window=None,
        se_interpolation_mode='nearest',
        stride_last=False,
        quantize=False,
    ):
        super(JasperBlock, self).__init__()

        if padding != "same":
            raise ValueError("currently only 'same' padding is supported")

        kernel_size_factor = float(kernel_size_factor)
        if type(kernel_size) in (list, tuple):
            kernel_size = [compute_new_kernel_size(k, kernel_size_factor) for k in kernel_size]
        else:
            kernel_size = compute_new_kernel_size(kernel_size, kernel_size_factor)

        padding_val = get_same_padding(kernel_size[0], stride[0], dilation[0])
        self.conv_mask = conv_mask
        self.separable = separable
        self.residual_mode = residual_mode
        self.se = se
        self.quantize = quantize

        inplanes_loop = inplanes
        conv = nn.ModuleList()

        for _ in range(repeat - 1):
            # Stride last means only the last convolution in block will have stride
            if stride_last:
                stride_val = [1]
            else:
                stride_val = stride

            conv.extend(
                self._get_conv_bn_layer(
                    inplanes_loop,
                    planes,
                    kernel_size=kernel_size,
                    stride=stride_val,
                    dilation=dilation,
                    padding=padding_val,
                    groups=groups,
                    heads=heads,
                    separable=separable,
                    normalization=normalization,
                    norm_groups=norm_groups,
                    quantize=quantize,
                )
            )

            conv.extend(self._get_act_dropout_layer(drop_prob=dropout, activation=activation))

            inplanes_loop = planes

        conv.extend(
            self._get_conv_bn_layer(
                inplanes_loop,
                planes,
                kernel_size=kernel_size,
                stride=stride,
                dilation=dilation,
                padding=padding_val,
                groups=groups,
                heads=heads,
                separable=separable,
                normalization=normalization,
                norm_groups=norm_groups,
                quantize=quantize,
            )
        )

        if se:
            conv.append(
                SqueezeExcite(
                    planes,
                    reduction_ratio=se_reduction_ratio,
                    context_window=se_context_window,
                    interpolation_mode=se_interpolation_mode,
                    activation=activation,
                    quantize=quantize,
                )
            )

        self.mconv = conv

        res_panes = residual_panes.copy()
        self.dense_residual = residual

        if residual:
            res_list = nn.ModuleList()

            if residual_mode == 'stride_add':
                stride_val = stride
            else:
                stride_val = [1]

            if len(residual_panes) == 0:
                res_panes = [inplanes]
                self.dense_residual = False
            for ip in res_panes:
                res = nn.ModuleList(
                    self._get_conv_bn_layer(
                        ip,
                        planes,
                        kernel_size=1,
                        normalization=normalization,
                        norm_groups=norm_groups,
                        stride=stride_val,
                        quantize=quantize,
                    )
                )

                res_list.append(res)

            self.res = res_list
            if PYTORCH_QUANTIZATION_AVAILABLE and self.quantize:
                self.residual_quantizer = quant_nn.TensorQuantizer(quant_nn.QuantConv2d.default_quant_desc_input)
            elif not PYTORCH_QUANTIZATION_AVAILABLE and quantize:
                raise ImportError(
                    "pytorch-quantization is not installed. Install from "
                    "https://github.com/NVIDIA/TensorRT/tree/master/tools/pytorch-quantization."
                )
        else:
            self.res = None

        self.mout = nn.Sequential(*self._get_act_dropout_layer(drop_prob=dropout, activation=activation))

    def _get_conv(
        self,
        in_channels,
        out_channels,
        kernel_size=11,
        stride=1,
        dilation=1,
        padding=0,
        bias=False,
        groups=1,
        heads=-1,
        separable=False,
        quantize=False,
    ):
        use_mask = self.conv_mask
        if use_mask:
            return MaskedConv1d(
                in_channels,
                out_channels,
                kernel_size,
                stride=stride,
                dilation=dilation,
                padding=padding,
                bias=bias,
                groups=groups,
                heads=heads,
                use_mask=use_mask,
                quantize=quantize,
            )
        else:
            if PYTORCH_QUANTIZATION_AVAILABLE and quantize:
                return quant_nn.QuantConv1d(
                    in_channels,
                    out_channels,
                    kernel_size,
                    stride=stride,
                    dilation=dilation,
                    padding=padding,
                    bias=bias,
                    groups=groups,
                )
            elif not PYTORCH_QUANTIZATION_AVAILABLE and quantize:
                raise ImportError(
                    "pytorch-quantization is not installed. Install from "
                    "https://github.com/NVIDIA/TensorRT/tree/master/tools/pytorch-quantization."
                )
            else:
                return nn.Conv1d(
                    in_channels,
                    out_channels,
                    kernel_size,
                    stride=stride,
                    dilation=dilation,
                    padding=padding,
                    bias=bias,
                    groups=groups,
                )

    def _get_conv_bn_layer(
        self,
        in_channels,
        out_channels,
        kernel_size=11,
        stride=1,
        dilation=1,
        padding=0,
        bias=False,
        groups=1,
        heads=-1,
        separable=False,
        normalization="batch",
        norm_groups=1,
        quantize=False,
    ):
        if norm_groups == -1:
            norm_groups = out_channels

        if separable:
            layers = [
                self._get_conv(
                    in_channels,
                    in_channels,
                    kernel_size,
                    stride=stride,
                    dilation=dilation,
                    padding=padding,
                    bias=bias,
                    groups=in_channels,
                    heads=heads,
                    quantize=quantize,
                ),
                self._get_conv(
                    in_channels,
                    out_channels,
                    kernel_size=1,
                    stride=1,
                    dilation=1,
                    padding=0,
                    bias=bias,
                    groups=groups,
                    quantize=quantize,
                ),
            ]
        else:
            layers = [
                self._get_conv(
                    in_channels,
                    out_channels,
                    kernel_size,
                    stride=stride,
                    dilation=dilation,
                    padding=padding,
                    bias=bias,
                    groups=groups,
                    quantize=quantize,
                )
            ]

        if normalization == "group":
            layers.append(nn.GroupNorm(num_groups=norm_groups, num_channels=out_channels))
        elif normalization == "instance":
            layers.append(nn.GroupNorm(num_groups=out_channels, num_channels=out_channels))
        elif normalization == "layer":
            layers.append(nn.GroupNorm(num_groups=1, num_channels=out_channels))
        elif normalization == "batch":
            layers.append(nn.BatchNorm1d(out_channels, eps=1e-3, momentum=0.1))
        else:
            raise ValueError(
                f"Normalization method ({normalization}) does not match" f" one of [batch, layer, group, instance]."
            )

        if groups > 1:
            layers.append(GroupShuffle(groups, out_channels))
        return layers

    def _get_act_dropout_layer(self, drop_prob=0.2, activation=None):
        if activation is None:
            activation = nn.Hardtanh(min_val=0.0, max_val=20.0)
        layers = [activation, nn.Dropout(p=drop_prob)]
        return layers

    def forward(self, input_: Tuple[List[Tensor], Optional[Tensor]]):
        """
        Forward pass of the module.

        Args:
            input_: The input is a tuple of two values - the preprocessed audio signal as well as the lengths
                of the audio signal. The audio signal is padded to the shape [B, D, T] and the lengths are
                a torch vector of length B.

        Returns:
            The output of the block after processing the input through `repeat` number of sub-blocks,
            as well as the lengths of the encoded audio after padding/striding.
        """
        # type: (Tuple[List[Tensor], Optional[Tensor]]) -> Tuple[List[Tensor], Optional[Tensor]] # nopep8
        lens_orig = None
        xs = input_[0]
        if len(input_) == 2:
            xs, lens_orig = input_

        # compute forward convolutions
        out = xs[-1]

        lens = lens_orig
        for i, l in enumerate(self.mconv):
            # if we're doing masked convolutions, we need to pass in and
            # possibly update the sequence lengths
            # if (i % 4) == 0 and self.conv_mask:
            if isinstance(l, MaskedConv1d):
                out, lens = l(out, lens)
            else:
                out = l(out)

        # compute the residuals
        if self.res is not None:
            for i, layer in enumerate(self.res):
                res_out = xs[i]
                for j, res_layer in enumerate(layer):
                    if isinstance(res_layer, MaskedConv1d):
                        res_out, _ = res_layer(res_out, lens_orig)
                    else:
                        res_out = res_layer(res_out)

                if self.residual_mode == 'add' or self.residual_mode == 'stride_add':
                    if PYTORCH_QUANTIZATION_AVAILABLE and self.quantize:
                        out = out + self.residual_quantizer(res_out)
                    elif not PYTORCH_QUANTIZATION_AVAILABLE and self.quantize:
                        raise ImportError(
                            "pytorch-quantization is not installed. Install from "
                            "https://github.com/NVIDIA/TensorRT/tree/master/tools/pytorch-quantization."
                        )
                    else:
                        out = out + res_out
                else:
                    out = torch.max(out, res_out)

        # compute the output
        out = self.mout(out)
        if self.res is not None and self.dense_residual:
            return xs + [out], lens

        return [out], lens
