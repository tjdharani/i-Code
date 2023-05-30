from abc import abstractmethod
from functools import partial
import math
from typing import Iterable

import random
import numpy as np
import torch as th
import torch.nn as nn
from torch import nn, einsum
import torch.nn.functional as F
from .diffusion_utils import \
    checkpoint, conv_nd, linear, avg_pool_nd, \
    zero_module, normalization, timestep_embedding

from .attention import SpatialTransformer
from core.models.make_a_video_pytorch import SpatioTemporalAttention
from einops import rearrange

from core.models.common.get_model import get_model, register

version = '0'
symbol = 'openai'

# dummy replace
def convert_module_to_f16(x):
    pass

def convert_module_to_f32(x):
    pass


class TimestepBlock(nn.Module):
    """
    Any module where forward() takes timestep embeddings as a second argument.
    """

    @abstractmethod
    def forward(self, x, emb):
        """
        Apply the module to `x` given `emb` timestep embeddings.
        """

class VideoSequential(nn.Sequential):
    """
    A sequential module that passes timestep embeddings to the children that
    support it as an extra input.
    """
    pass
    
class TimestepEmbedSequential(nn.Sequential, TimestepBlock):
    """
    A sequential module that passes timestep embeddings to the children that
    support it as an extra input.
    """

    def forward(self, x, emb=None, context=None):
        is_video = (x.ndim == 5)
        if is_video:
            num_frames = x.shape[2]
            if emb is not None:
                emb = emb.unsqueeze(1).repeat(1, num_frames, 1)
                emb = rearrange(emb, 'b t c -> (b t) c')
            if context is not None:
                context_vid = context.unsqueeze(1).repeat(1, num_frames, 1, 1)
                context_vid = rearrange(context_vid, 'b t n c -> (b t) n c')

        for layer in self:
            if isinstance(layer, TimestepBlock):
                x = layer(x, emb)
            elif isinstance(layer, SpatialTransformer):
                if is_video:
                    x = rearrange(x, 'b c t h w -> (b t) c h w ')
                    x = layer(x, context_vid)
                    x = rearrange(x, '(b t) c h w -> b c t h w', t=num_frames)
                else:
                    x = layer(x, context)
            elif isinstance(layer, SpatioTemporalAttention):
                x = layer(x)
            elif isinstance(layer, VideoSequential) or isinstance(layer, nn.ModuleList):
                x = layer[0](x, emb)    
                x = layer[1](x)    
            else:
                if is_video:
                    x = rearrange(x, 'b c t h w -> (b t) c h w ')
                x = layer(x)
                if is_video:
                    x = rearrange(x, '(b t) c h w -> b c t h w', t=num_frames)
        return x


class Upsample(nn.Module):
    """
    An upsampling layer with an optional convolution.
    :param channels: channels in the inputs and outputs.
    :param use_conv: a bool determining if a convolution is applied.
    :param dims: determines if the signal is 1D, 2D, or 3D. If 3D, then
                 upsampling occurs in the inner-two dimensions.
    """

    def __init__(self, channels, use_conv, dims=2, out_channels=None, padding=1):
        super().__init__()
        self.channels = channels
        self.out_channels = out_channels or channels
        self.use_conv = use_conv
        self.dims = dims
        if use_conv:
            self.conv = conv_nd(dims, self.channels, self.out_channels, 3, padding=padding)

    def forward(self, x):
        assert x.shape[1] == self.channels
        if self.dims == 3:
            x = F.interpolate(
                x, (x.shape[2], x.shape[3] * 2, x.shape[4] * 2), mode="nearest"
            )
        else:
            x = F.interpolate(x, scale_factor=2, mode="nearest")
        if self.use_conv:
            x = self.conv(x)
        return x


class TransposedUpsample(nn.Module):
    'Learned 2x upsampling without padding'
    def __init__(self, channels, out_channels=None, ks=5):
        super().__init__()
        self.channels = channels
        self.out_channels = out_channels or channels

        self.up = nn.ConvTranspose2d(self.channels,self.out_channels,kernel_size=ks,stride=2)

    def forward(self,x):
        return self.up(x)


class Downsample(nn.Module):
    """
    A downsampling layer with an optional convolution.
    :param channels: channels in the inputs and outputs.
    :param use_conv: a bool determining if a convolution is applied.
    :param dims: determines if the signal is 1D, 2D, or 3D. If 3D, then
                 downsampling occurs in the inner-two dimensions.
    """

    def __init__(self, channels, use_conv, dims=2, out_channels=None, padding=1):
        super().__init__()
        self.channels = channels
        self.out_channels = out_channels or channels
        self.use_conv = use_conv
        self.dims = dims
        stride = 2 if dims != 3 else (1, 2, 2)
        if use_conv:
            self.op = conv_nd(
                dims, self.channels, self.out_channels, 3, stride=stride, padding=padding
            )
        else:
            assert self.channels == self.out_channels
            self.op = avg_pool_nd(dims, kernel_size=stride, stride=stride)

    def forward(self, x):
        assert x.shape[1] == self.channels
        return self.op(x)

    
class ConnectorOut(nn.Module):
    """
    A residual block that can optionally change the number of channels.
    :param channels: the number of input channels.
    :param emb_channels: the number of timestep embedding channels.
    :param dropout: the rate of dropout.
    :param out_channels: if specified, the number of out channels.
    :param use_conv: if True and out_channels is specified, use a spatial
        convolution instead of a smaller 1x1 convolution to change the
        channels in the skip connection.
    :param dims: determines if the signal is 1D, 2D, or 3D.
    :param use_checkpoint: if True, use gradient checkpointing on this module.
    :param up: if True, use this block for upsampling.
    :param down: if True, use this block for downsampling.
    """

    def __init__(
        self,
        channels,
        dropout=0,
        out_channels=None,
        use_conv=False,
        dims=2,
        use_checkpoint=False,
        use_temporal_attention=False,
    ):
        super().__init__()
        self.channels = channels
        self.dropout = dropout
        self.out_channels = out_channels or channels
        self.use_conv = use_conv
        self.use_checkpoint = use_checkpoint

        self.in_layers = nn.Sequential(
            normalization(channels),
            nn.SiLU(),
            conv_nd(dims, channels, self.out_channels, 3, padding=1),
        )
        self.out_layers = nn.Sequential(
            normalization(self.out_channels),
            nn.SiLU(),
            nn.Dropout(p=dropout),
            zero_module(
                conv_nd(dims, self.out_channels, self.out_channels, 3, padding=1)
            ),
        )
        self.use_temporal_attention = use_temporal_attention
        if use_temporal_attention:
            self.temporal_attention = SpatioTemporalAttention(
                                dim = self.out_channels,
                                dim_head = self.out_channels // 4,
                                heads = 8,
                                use_resnet = False,
                              )
        if self.out_channels == channels:
            self.skip_connection = nn.Identity()
        elif use_conv:
            self.skip_connection = conv_nd(
                dims, channels, self.out_channels, 3, padding=1
            )
        else:
            self.skip_connection = conv_nd(dims, channels, self.out_channels, 1)

    def forward(self, x):
        """
        Apply the block to a Tensor, conditioned on a timestep embedding.
        :param x: an [N x C x ...] Tensor of features.
        :return: an [N x C x ...] Tensor of outputs.
        """
        return checkpoint(
            self._forward, (x, ), self.parameters(), self.use_checkpoint
        )

    def _forward(self, x):
        is_video = x.ndim == 5
        if is_video:
            num_frames = x.shape[2]
            if self.use_temporal_attention:
                x = self.temporal_attention(x)
            x = rearrange(x, 'b c t h w -> (b t) c h w ')
                    
        h = self.in_layers(x)
        h = self.out_layers(h)
        out = self.skip_connection(x) + h
        if is_video:
            out = rearrange(out, '(b t) c h w -> b c t h w', t=num_frames)
            out = out.mean(2)
        return out.mean([2, 3]).unsqueeze(1)

    
class ResBlock(TimestepBlock):
    """
    A residual block that can optionally change the number of channels.
    :param channels: the number of input channels.
    :param emb_channels: the number of timestep embedding channels.
    :param dropout: the rate of dropout.
    :param out_channels: if specified, the number of out channels.
    :param use_conv: if True and out_channels is specified, use a spatial
        convolution instead of a smaller 1x1 convolution to change the
        channels in the skip connection.
    :param dims: determines if the signal is 1D, 2D, or 3D.
    :param use_checkpoint: if True, use gradient checkpointing on this module.
    :param up: if True, use this block for upsampling.
    :param down: if True, use this block for downsampling.
    """

    def __init__(
        self,
        channels,
        emb_channels,
        dropout,
        out_channels=None,
        use_conv=False,
        use_scale_shift_norm=False,
        dims=2,
        use_checkpoint=False,
        up=False,
        down=False,
    ):
        super().__init__()
        self.channels = channels
        self.emb_channels = emb_channels
        self.dropout = dropout
        self.out_channels = out_channels or channels
        self.use_conv = use_conv
        self.use_checkpoint = use_checkpoint
        self.use_scale_shift_norm = use_scale_shift_norm

        self.in_layers = nn.Sequential(
            normalization(channels),
            nn.SiLU(),
            conv_nd(dims, channels, self.out_channels, 3, padding=1),
        )

        self.updown = up or down

        if up:
            self.h_upd = Upsample(channels, False, dims)
            self.x_upd = Upsample(channels, False, dims)
        elif down:
            self.h_upd = Downsample(channels, False, dims)
            self.x_upd = Downsample(channels, False, dims)
        else:
            self.h_upd = self.x_upd = nn.Identity()

        self.emb_layers = nn.Sequential(
            nn.SiLU(),
            linear(
                emb_channels,
                2 * self.out_channels if use_scale_shift_norm else self.out_channels,
            ),
        )
        self.out_layers = nn.Sequential(
            normalization(self.out_channels),
            nn.SiLU(),
            nn.Dropout(p=dropout),
            zero_module(
                conv_nd(dims, self.out_channels, self.out_channels, 3, padding=1)
            ),
        )

        if self.out_channels == channels:
            self.skip_connection = nn.Identity()
        elif use_conv:
            self.skip_connection = conv_nd(
                dims, channels, self.out_channels, 3, padding=1
            )
        else:
            self.skip_connection = conv_nd(dims, channels, self.out_channels, 1)

    def forward(self, x, emb):
        """
        Apply the block to a Tensor, conditioned on a timestep embedding.
        :param x: an [N x C x ...] Tensor of features.
        :param emb: an [N x emb_channels] Tensor of timestep embeddings.
        :return: an [N x C x ...] Tensor of outputs.
        """
        return checkpoint(
            self._forward, (x, emb), self.parameters(), self.use_checkpoint
        )


    def _forward(self, x, emb):
        is_video = x.ndim == 5
        if is_video:
            num_frames = x.shape[2]
            x = rearrange(x, 'b c t h w -> (b t) c h w ')
                    
        if self.updown:
            in_rest, in_conv = self.in_layers[:-1], self.in_layers[-1]
            h = in_rest(x)
            h = self.h_upd(h)
            x = self.x_upd(x)
            h = in_conv(h)
        else:
            h = self.in_layers(x)
        emb_out = self.emb_layers(emb)
        while len(emb_out.shape) < len(h.shape):
            emb_out = emb_out[..., None]
        if self.use_scale_shift_norm:
            out_norm, out_rest = self.out_layers[0], self.out_layers[1:]
            scale, shift = th.chunk(emb_out, 2, dim=1)
            h = out_norm(h) * (1 + scale) + shift
            h = out_rest(h)
        else:
            h = h + emb_out
            h = self.out_layers(h)
            
        out = self.skip_connection(x) + h
        if is_video:
            out = rearrange(out, '(b t) c h w -> b c t h w', t=num_frames)
        return out


class AttentionBlock(nn.Module):
    """
    An attention block that allows spatial positions to attend to each other.
    Originally ported from here, but adapted to the N-d case.
    https://github.com/hojonathanho/diffusion/blob/1e0dceb3b3495bbe19116a5e1b3596cd0706c543/diffusion_tf/models/unet.py#L66.
    """

    def __init__(
        self,
        channels,
        num_heads=1,
        num_head_channels=-1,
        use_checkpoint=False,
        use_new_attention_order=False,
    ):
        super().__init__()
        self.channels = channels
        if num_head_channels == -1:
            self.num_heads = num_heads
        else:
            assert (
                channels % num_head_channels == 0
            ), f"q,k,v channels {channels} is not divisible by num_head_channels {num_head_channels}"
            self.num_heads = channels // num_head_channels
        self.use_checkpoint = use_checkpoint
        self.norm = normalization(channels)
        self.qkv = conv_nd(1, channels, channels * 3, 1)
        if use_new_attention_order:
            # split qkv before split heads
            self.attention = QKVAttention(self.num_heads)
        else:
            # split heads before split qkv
            self.attention = QKVAttentionLegacy(self.num_heads)

        self.proj_out = zero_module(conv_nd(1, channels, channels, 1))

    def forward(self, x):
        return checkpoint(self._forward, (x,), self.parameters(), True)   # TODO: check checkpoint usage, is True # TODO: fix the .half call!!!
        #return pt_checkpoint(self._forward, x)  # pytorch

    def _forward(self, x):
        b, c, *spatial = x.shape
        x = x.reshape(b, c, -1)
        qkv = self.qkv(self.norm(x))
        h = self.attention(qkv)
        h = self.proj_out(h)
        return (x + h).reshape(b, c, *spatial)


def count_flops_attn(model, _x, y):
    """
    A counter for the `thop` package to count the operations in an
    attention operation.
    Meant to be used like:
        macs, params = thop.profile(
            model,
            inputs=(inputs, timestamps),
            custom_ops={QKVAttention: QKVAttention.count_flops},
        )
    """
    b, c, *spatial = y[0].shape
    num_spatial = int(np.prod(spatial))
    # We perform two matmuls with the same number of ops.
    # The first computes the weight matrix, the second computes
    # the combination of the value vectors.
    matmul_ops = 2 * b * (num_spatial ** 2) * c
    model.total_ops += th.DoubleTensor([matmul_ops])


class QKVAttentionLegacy(nn.Module):
    """
    A module which performs QKV attention. Matches legacy QKVAttention + input/ouput heads shaping
    """

    def __init__(self, n_heads):
        super().__init__()
        self.n_heads = n_heads

    def forward(self, qkv):
        """
        Apply QKV attention.
        :param qkv: an [N x (H * 3 * C) x T] tensor of Qs, Ks, and Vs.
        :return: an [N x (H * C) x T] tensor after attention.
        """
        bs, width, length = qkv.shape
        assert width % (3 * self.n_heads) == 0
        ch = width // (3 * self.n_heads)
        q, k, v = qkv.reshape(bs * self.n_heads, ch * 3, length).split(ch, dim=1)
        scale = 1 / math.sqrt(math.sqrt(ch))
        weight = th.einsum(
            "bct,bcs->bts", q * scale, k * scale
        )  # More stable with f16 than dividing afterwards
        weight = th.softmax(weight, dim=-1).type(weight.dtype)
        a = th.einsum("bts,bcs->bct", weight, v)
        return a.reshape(bs, -1, length)

    @staticmethod
    def count_flops(model, _x, y):
        return count_flops_attn(model, _x, y)


class QKVAttention(nn.Module):
    """
    A module which performs QKV attention and splits in a different order.
    """

    def __init__(self, n_heads):
        super().__init__()
        self.n_heads = n_heads

    def forward(self, qkv):
        """
        Apply QKV attention.
        :param qkv: an [N x (3 * H * C) x T] tensor of Qs, Ks, and Vs.
        :return: an [N x (H * C) x T] tensor after attention.
        """
        bs, width, length = qkv.shape
        assert width % (3 * self.n_heads) == 0
        ch = width // (3 * self.n_heads)
        q, k, v = qkv.chunk(3, dim=1)
        scale = 1 / math.sqrt(math.sqrt(ch))
        weight = th.einsum(
            "bct,bcs->bts",
            (q * scale).view(bs * self.n_heads, ch, length),
            (k * scale).view(bs * self.n_heads, ch, length),
        )  # More stable with f16 than dividing afterwards
        weight = th.softmax(weight, dim=-1).type(weight.dtype)
        a = th.einsum("bts,bcs->bct", weight, v.reshape(bs * self.n_heads, ch, length))
        return a.reshape(bs, -1, length)

    @staticmethod
    def count_flops(model, _x, y):
        return count_flops_attn(model, _x, y)


###########
# VD Unet #
###########

from functools import partial

@register('openai_unet_2d', version)
class UNetModel2D(nn.Module):
    def __init__(self,
                 input_channels,
                 model_channels,
                 output_channels,
                 context_dim=768,
                 num_noattn_blocks=(2, 2, 2, 2),
                 channel_mult=(1, 2, 4, 8),
                 with_attn=[True, True, True, False],
                 channel_mult_connector=(1, 2, 4),
                 num_noattn_blocks_connector=(1, 1, 1),
                 with_connector=[True, True, True, False],
                 num_heads=8,
                 use_checkpoint=True, 
                 use_video_architecture=False,
                 video_dim_scale_factor=4):

        super().__init__()
        ResBlockPreset = partial(
            ResBlock, dropout=0, dims=2, use_checkpoint=use_checkpoint, 
            use_scale_shift_norm=False)
 
        self.input_channels = input_channels
        self.model_channels = model_channels
        self.num_noattn_blocks = num_noattn_blocks
        self.channel_mult = channel_mult
        self.num_heads = num_heads

        ##################
        # Time embedding #
        ##################

        time_embed_dim = model_channels * 4
        self.time_embed = nn.Sequential(
            linear(model_channels, time_embed_dim),
            nn.SiLU(),
            linear(time_embed_dim, time_embed_dim),)
        
        ##################
        #   Connector    #
        ##################
        
        current_channel = model_channels // 2
        self.connecters_out = nn.ModuleList([TimestepEmbedSequential(
                nn.Conv2d(input_channels, current_channel, 3, padding=1, bias=True))])
        for level_idx, mult in enumerate(channel_mult_connector):
            for _ in range(num_noattn_blocks_connector[level_idx]):
                if use_video_architecture:
                    layers = [nn.ModuleList([
                        ResBlockPreset(
                            current_channel, time_embed_dim,
                            out_channels = mult * model_channels),
                        SpatioTemporalAttention(
                                dim = mult * model_channels,
                                dim_head = mult * model_channels // video_dim_scale_factor,
                                heads = 8
                              )])]
                else:
                    layers = [
                        ResBlockPreset(
                            current_channel, time_embed_dim,
                            out_channels = mult * model_channels)]

                current_channel = mult * model_channels
                self.connecters_out.append(TimestepEmbedSequential(*layers))

            if level_idx != len(channel_mult_connector) - 1:
                self.connecters_out.append(
                    TimestepEmbedSequential(
                        Downsample(
                            current_channel, use_conv=True, 
                            dims=2, out_channels=current_channel,)))
        connector_out_channels = current_channel
        
        ################
        # input_blocks #
        ################
        current_channel = model_channels
        input_blocks = [
            TimestepEmbedSequential(
                nn.Conv2d(input_channels, model_channels, 3, padding=1, bias=True))]
        input_block_channels = [current_channel]
        
        input_block_connecters_in = [None]
        
        for level_idx, mult in enumerate(channel_mult):
            for _ in range(self.num_noattn_blocks[level_idx]):
                if use_video_architecture:
                    layers = [nn.ModuleList([
                        ResBlockPreset(
                            current_channel, time_embed_dim,
                            out_channels = mult * model_channels),
                        SpatioTemporalAttention(
                                dim = mult * model_channels,
                                dim_head = mult * model_channels // video_dim_scale_factor,
                                heads = 8
                              )])]
                else:
                    layers = [
                        ResBlockPreset(
                            current_channel, time_embed_dim,
                            out_channels = mult * model_channels)]

                current_channel = mult * model_channels
                dim_head = current_channel // num_heads
                if with_attn[level_idx]:
                    layers += [
                        SpatialTransformer(
                            current_channel, num_heads, dim_head, 
                            depth=1, context_dim=context_dim)]
                    

                input_blocks += [TimestepEmbedSequential(*layers)]
                input_block_channels.append(current_channel)
                if with_connector[level_idx]:
                    input_block_connecters_in.append(
                        TimestepEmbedSequential(*[SpatialTransformer(
                            current_channel, num_heads, dim_head, 
                            depth=1, context_dim=connector_out_channels)])
                    )
                else:
                    input_block_connecters_in.append(None)

            if level_idx != len(channel_mult) - 1:
                input_blocks += [
                    TimestepEmbedSequential(
                        Downsample(
                            current_channel, use_conv=True, 
                            dims=2, out_channels=current_channel,))]
                input_block_channels.append(current_channel)
                input_block_connecters_in.append(None)

        self.input_blocks = nn.ModuleList(input_blocks)
        self.input_block_connecters_in = nn.ModuleList(input_block_connecters_in)

        
        #################
        # middle_blocks #
        #################
        
        if use_video_architecture:
            layer1 = nn.ModuleList([
                ResBlockPreset(
                    current_channel, time_embed_dim),
                SpatioTemporalAttention(
                        dim = current_channel,
                        dim_head = current_channel // video_dim_scale_factor,
                        heads = 8
                      )])
            layer2 = nn.ModuleList([
                ResBlockPreset(
                    current_channel, time_embed_dim),
                SpatioTemporalAttention(
                        dim = current_channel,
                        dim_head = current_channel // video_dim_scale_factor,
                        heads = 8
                      )])
        else:
            layer1 = ResBlockPreset(
                current_channel, time_embed_dim)
            layer2 = ResBlockPreset(
                current_channel, time_embed_dim)

        middle_block = [
            layer1,
            SpatialTransformer(
                current_channel, num_heads, dim_head, 
                depth=1, context_dim=context_dim),
            layer2]

        self.middle_block = TimestepEmbedSequential(*middle_block)

        #################
        # output_blocks #
        #################
        output_blocks = []
        output_block_connecters_out = []
        output_block_connecters_in = []
        for level_idx, mult in list(enumerate(channel_mult))[::-1]:
            for block_idx in range(self.num_noattn_blocks[level_idx] + 1):
                extra_channel = input_block_channels.pop()
                if use_video_architecture:
                    layers = [nn.ModuleList([
                        ResBlockPreset(
                            current_channel + extra_channel,
                            time_embed_dim,
                            out_channels = model_channels * mult),
                        SpatioTemporalAttention(
                                dim = mult * model_channels,
                                dim_head = mult * model_channels // video_dim_scale_factor,
                                heads = 8
                              )])]
                else:
                    layers = [
                        ResBlockPreset(
                            current_channel + extra_channel,
                            time_embed_dim,
                            out_channels = model_channels * mult) ]
                
                current_channel = model_channels * mult
                dim_head = current_channel // num_heads

                if with_attn[level_idx]:
                    layers += [
                        SpatialTransformer(
                            current_channel, num_heads, dim_head, 
                            depth=1, context_dim=context_dim)]
                if with_connector[level_idx]:
                    output_block_connecters_in.append(
                        TimestepEmbedSequential(*[SpatialTransformer(
                            current_channel, num_heads, dim_head, 
                            depth=1, context_dim=connector_out_channels)])
                    )
                else:
                    output_block_connecters_in.append(None)
    

                if level_idx!=0 and block_idx==self.num_noattn_blocks[level_idx]:
                    layers += [
                        Upsample(
                            current_channel, use_conv=True, 
                            dims=2, out_channels=current_channel)]
 
                output_blocks += [TimestepEmbedSequential(*layers)]

        self.output_blocks = nn.ModuleList(output_blocks)
        self.output_block_connecters_in = nn.ModuleList(output_block_connecters_in)

        self.out = nn.Sequential(
            normalization(current_channel),
            nn.SiLU(),
            zero_module(nn.Conv2d(model_channels, output_channels, 3, padding=1)),)

    def forward(self, x, timesteps=None, context=None):
        hs = []
        t_emb = timestep_embedding(timesteps, self.model_channels, repeat_only=False)
        emb = self.time_embed(t_emb)

        h = x
        is_video = h.ndim == 5
            
        for module in self.input_blocks:
            h = module(h, emb, context)
            hs.append(h)
        h = self.middle_block(h, emb, context)
        for module in self.output_blocks:
            h = th.cat([h, hs.pop()], dim=1)
            h = module(h, emb, context)
        return self.out(h)

class FCBlock(TimestepBlock):
    def __init__(
        self,
        channels,
        emb_channels,
        dropout,
        out_channels=None,
        use_checkpoint=False,
    ):
        super().__init__()
        self.channels = channels
        self.emb_channels = emb_channels
        self.dropout = dropout
        self.out_channels = out_channels or channels
        self.use_checkpoint = use_checkpoint

        self.in_layers = nn.Sequential(
            normalization(channels),
            nn.SiLU(),
            nn.Conv2d(channels, self.out_channels, 1, padding=0),)

        self.emb_layers = nn.Sequential(
            nn.SiLU(),
            linear(emb_channels, self.out_channels,),)
        self.out_layers = nn.Sequential(
            normalization(self.out_channels),
            nn.SiLU(),
            nn.Dropout(p=dropout),
            zero_module(nn.Conv2d(self.out_channels, self.out_channels, 1, padding=0)),
        )

        if self.out_channels == channels:
            self.skip_connection = nn.Identity()
        else:
            self.skip_connection = nn.Conv2d(channels, self.out_channels, 1, padding=0)

    def forward(self, x, emb):
        if len(x.shape) == 2:
            x = x[:, :, None, None]
        elif len(x.shape) == 4:
            pass
        else:
            raise ValueError
        y = checkpoint(
            self._forward, (x, emb), self.parameters(), self.use_checkpoint)
        if len(x.shape) == 2:
            return y[:, :, 0, 0]
        elif len(x.shape) == 4:
            return y

    def _forward(self, x, emb):
        h = self.in_layers(x)
        emb_out = self.emb_layers(emb).type(h.dtype)
        while len(emb_out.shape) < len(h.shape):
            emb_out = emb_out[..., None]
        h = h + emb_out
        h = self.out_layers(h)
        return self.skip_connection(x) + h


class Linear_MultiDim(nn.Linear):
    def __init__(self, in_features, out_features, *args, **kwargs):
        
        in_features = [in_features] if isinstance(in_features, int) else list(in_features)
        out_features = [out_features] if isinstance(out_features, int) else list(out_features)
        self.in_features_multidim = in_features
        self.out_features_multidim = out_features
        super().__init__(
            np.array(in_features).prod(), 
            np.array(out_features).prod(), 
            *args, **kwargs)

    def forward(self, x):
        shape = x.shape
        n = len(self.in_features_multidim)
        x = x.reshape(*shape[0:-n], self.in_features)
        y = super().forward(x)
        y = y.view(*shape[0:-n], *self.out_features_multidim)
        return y

class FCBlock_MultiDim(FCBlock):
    def __init__(
            self,
            channels,
            emb_channels,
            dropout,
            out_channels=None,
            use_checkpoint=False,):
        channels = [channels] if isinstance(channels, int) else list(channels)
        channels_all = np.array(channels).prod()
        self.channels_multidim = channels

        if out_channels is not None:
            out_channels = [out_channels] if isinstance(out_channels, int) else list(out_channels)
            out_channels_all = np.array(out_channels).prod()
            self.out_channels_multidim = out_channels
        else:
            out_channels_all = channels_all
            self.out_channels_multidim = self.channels_multidim

        self.channels = channels
        super().__init__(
            channels = channels_all,
            emb_channels = emb_channels,
            dropout = dropout,
            out_channels = out_channels_all,
            use_checkpoint = use_checkpoint,)

    def forward(self, x, emb):
        shape = x.shape
        n = len(self.channels_multidim)
        x = x.reshape(*shape[0:-n], self.channels, 1, 1)
        x = x.view(-1, self.channels, 1, 1)
        y = checkpoint(
            self._forward, (x, emb), self.parameters(), self.use_checkpoint)
        y = y.view(*shape[0:-n], -1)
        y = y.view(*shape[0:-n], *self.out_channels_multidim)
        return y

@register('openai_unet_0dmd', version)
class UNetModel0D_MultiDim(nn.Module):
    def __init__(self,
                 input_channels,
                 model_channels,
                 output_channels,
                 context_dim=768,
                 num_noattn_blocks=(2, 2, 2, 2),
                 channel_mult=(1, 2, 4, 8),
                 second_dim=(4, 4, 4, 4),
                 with_attn=[True, True, True, False],
                 channel_mult_connector=(1, 2, 4),
                 num_noattn_blocks_connector=(1, 1, 1),
                 second_dim_connector=(4, 4, 4),
                 with_connector=[True, True, True, False],
                 num_heads=8,
                 use_checkpoint=True, ):

        super().__init__()

        FCBlockPreset = partial(FCBlock_MultiDim, dropout=0, use_checkpoint=use_checkpoint)
 
        self.input_channels = input_channels
        self.model_channels = model_channels
        self.num_noattn_blocks = num_noattn_blocks
        self.channel_mult = channel_mult
        self.second_dim = second_dim
        self.num_heads = num_heads
        
        ##################
        # Time embedding #
        ##################

        time_embed_dim = model_channels * 4
        self.time_embed = nn.Sequential(
            linear(model_channels, time_embed_dim),
            nn.SiLU(),
            linear(time_embed_dim, time_embed_dim),)

        ##################
        #   Connector    #
        ##################
        
        sdim = second_dim[0]
        current_channel = [model_channels//2, sdim, 1]
        self.connecters_out = nn.ModuleList([TimestepEmbedSequential(
                Linear_MultiDim([input_channels, 1, 1], current_channel, bias=True))])
        for level_idx, (mult, sdim) in enumerate(zip(channel_mult_connector, second_dim_connector)):
            for _ in range(num_noattn_blocks_connector[level_idx]):
                layers = [
                    FCBlockPreset(
                        current_channel, 
                        time_embed_dim,
                        out_channels = [mult*model_channels, sdim, 1],)]

                current_channel = [mult*model_channels, sdim, 1]
                self.connecters_out += [TimestepEmbedSequential(*layers)]
                    
            if level_idx != len(channel_mult_connector) - 1:
                self.connecters_out += [
                    TimestepEmbedSequential(
                        Linear_MultiDim(current_channel, current_channel, bias=True, ))]
        connector_out_channels = current_channel[0]
        
        
        ################
        # input_blocks #
        ################
        sdim = second_dim[0]
        current_channel = [model_channels, sdim, 1]
        input_blocks = [
            TimestepEmbedSequential(
                Linear_MultiDim([input_channels, 1, 1], current_channel, bias=True))]
        input_block_channels = [current_channel]
        input_block_connecters_in = [None]
    
        for level_idx, (mult, sdim) in enumerate(zip(channel_mult, second_dim)):
            for _ in range(self.num_noattn_blocks[level_idx]):
                layers = [
                    FCBlockPreset(
                        current_channel, 
                        time_embed_dim,
                        out_channels = [mult*model_channels, sdim, 1],)]

                current_channel = [mult*model_channels, sdim, 1]
                dim_head = current_channel[0] // num_heads
                if with_attn[level_idx]:
                    layers += [
                        SpatialTransformer(
                            current_channel[0], num_heads, dim_head, 
                            depth=1, context_dim=context_dim, )]

                input_blocks += [TimestepEmbedSequential(*layers)]
                input_block_channels.append(current_channel)

                if with_connector[level_idx]:
                    input_block_connecters_in.append(
                        TimestepEmbedSequential(*[SpatialTransformer(
                            current_channel[0], num_heads, dim_head, 
                            depth=1, context_dim=connector_out_channels)])
                    )
                else:
                    input_block_connecters_in.append(None)
                    
            if level_idx != len(channel_mult) - 1:
                input_blocks += [
                    TimestepEmbedSequential(
                        Linear_MultiDim(current_channel, current_channel, bias=True, ))]
                input_block_channels.append(current_channel)
                input_block_connecters_in.append(None)

        self.input_blocks = nn.ModuleList(input_blocks)
        self.input_block_connecters_in = nn.ModuleList(input_block_connecters_in)

        #################
        # middle_blocks #
        #################
        middle_block = [
            FCBlockPreset(
                current_channel, time_embed_dim, ),
            SpatialTransformer(
                current_channel[0], num_heads, dim_head, 
                depth=1, context_dim=context_dim, ),
            FCBlockPreset(
                current_channel, time_embed_dim, ),]
        self.middle_block = TimestepEmbedSequential(*middle_block)

        #################
        # output_blocks #
        #################
        output_blocks = []
        output_block_connecters_in = []
        for level_idx, (mult, sdim) in list(enumerate(zip(channel_mult, second_dim)))[::-1]:
            for block_idx in range(self.num_noattn_blocks[level_idx] + 1):
                extra_channel = input_block_channels.pop()
                layers = [
                    FCBlockPreset(
                        [current_channel[0] + extra_channel[0]] + current_channel[1:],
                        time_embed_dim,
                        out_channels = [mult*model_channels, sdim, 1], )]

                current_channel = [mult*model_channels, sdim, 1]
                dim_head = current_channel[0] // num_heads
                if with_attn[level_idx]:
                    layers += [
                        SpatialTransformer(
                            current_channel[0], num_heads, dim_head, 
                            depth=1, context_dim=context_dim,)]

                if with_connector[level_idx]:
                    output_block_connecters_in.append(
                        TimestepEmbedSequential(*[SpatialTransformer(
                            current_channel[0], num_heads, dim_head, 
                            depth=1, context_dim=connector_out_channels)])
                    )
                else:
                    output_block_connecters_in.append(None)
                    
                if level_idx!=0 and block_idx==self.num_noattn_blocks[level_idx]:
                    layers += [
                        Linear_MultiDim(current_channel, current_channel, bias=True, )]

                output_blocks += [TimestepEmbedSequential(*layers)]

        self.output_blocks = nn.ModuleList(output_blocks)
        self.output_block_connecters_in = nn.ModuleList(output_block_connecters_in)
        
        self.out = nn.Sequential(
            normalization(current_channel[0]),
            nn.SiLU(),
            zero_module(Linear_MultiDim(current_channel, [output_channels, 1, 1], bias=True, )),)

    def forward(self, x, timesteps=None, context=None):
        hs = []
        t_emb = timestep_embedding(timesteps, self.model_channels, repeat_only=False)
        emb = self.time_embed(t_emb)

        h = x
        for module in self.input_blocks:
            h = module(h, emb, context)
            hs.append(h)
        h = self.middle_block(h, emb, context)
        for module in self.output_blocks:
            h = th.cat([h, hs.pop()], dim=1)
            h = module(h, emb, context)
        return self.out(h)

    
@register('openai_unet_vd', version)
class UNetModelVD(nn.Module):
    def __init__(self,
                 unet_image_cfg,  
                 unet_text_cfg, 
                 unet_audio_cfg):

        super().__init__()
        self.unet_image = get_model()(unet_image_cfg)
        self.unet_text = get_model()(unet_text_cfg)
        self.unet_audio = get_model()(unet_audio_cfg)

        self.model_channels = self.unet_image.model_channels
        
    def forward(self, x, timesteps, c0, c1, c2, xtype, mixed_ratio=None, mixed_ratio_c2=None):
        
        # Conditioning
        if c2 is not None and c1 is not None:
            context = c0*(1-mixed_ratio-mixed_ratio_c2) + c1*mixed_ratio + c2*mixed_ratio_c2
        elif c1 is not None:  
            context = c0*mixed_ratio + c1*(1-mixed_ratio)
        else:
            context = c0

        # Prepare inputs
        hs = []
        x = [temp.cuda() for temp in x]
        timesteps = timesteps.cuda()
        context = context.cuda()
        t_emb = timestep_embedding(timesteps, self.model_channels, repeat_only=False).to(x[0])
        
        emb_image = self.unet_image.time_embed(t_emb)
        emb_text = self.unet_text.time_embed(t_emb)
        emb_audio = self.unet_audio.time_embed(t_emb)

        for i in range(len(xtype)):
            if xtype[i] == 'text':
                x[i] = x[i][:, :, None, None]

        # Environment encoders
        h_con = [temp for temp in x]        
        for i_con_in, t_con_in, a_con_in in zip(
            self.unet_image.connecters_out, self.unet_text.connecters_out, self.unet_audio.connecters_out,
            ):
            for i, xtype_i in enumerate(xtype):
                if xtype_i == 'audio':
                    h_con[i] = a_con_in(h_con[i], emb_audio, context)        
                elif xtype_i in ['video', 'image']:
                    h_con[i] = i_con_in(h_con[i], emb_image, context)
                elif xtype_i == 'text':
                    h_con[i] = t_con_in(h_con[i], emb_text, context)   
                else:
                    raise
        for i in range(len(h_con)):
            if h_con[i].ndim == 5:
                h_con[i] = h_con[i].mean(2).mean(2).mean(2).unsqueeze(1)
            else:
                h_con[i] = h_con[i].mean(2).mean(2).unsqueeze(1)
            h_con[i] = h_con[i] / th.norm(h_con[i], dim=-1, keepdim=True)

        context = [context, context]
        
        # Joint / single generation
        h = x
        for (i_module, t_module, a_module,
            i_con_in, t_con_in, a_con_in) \
        in zip(
            self.unet_image.input_blocks, self.unet_text.input_blocks, self.unet_audio.input_blocks,
            self.unet_image.input_block_connecters_in, self.unet_text.input_block_connecters_in, self.unet_audio.input_block_connecters_in, 
            ):
            h = [h_i for h_i in h]
            for i, xtype_i in enumerate(xtype):
                if xtype_i == 'audio':
                    h[i] = a_module(h[i], emb_audio, context[i])
                elif xtype_i in ['video', 'image']:
                    h[i] = i_module(h[i], emb_image, context[i])
                elif xtype_i == 'text':
                    h[i] = t_module(h[i], emb_text, context[i])   
                else:
                    raise

            if i_con_in is not None:
                for i, xtype_i in enumerate(xtype):
                    if xtype_i == 'audio':
                        h[i] = a_con_in(h[i], context=h_con[i])
                    elif xtype_i in ['video', 'image']:
                        h[i] = i_con_in(h[i], context=h_con[i])
                    elif xtype_i == 'text':
                        h[i] = t_con_in(h[i], context=h_con[i])  
                    else:
                        raise

            hs.append(h)            


        for i, xtype_i in enumerate(xtype):
            if xtype_i == 'audio':
                h[i] = self.unet_audio.middle_block(h[i], emb_audio, context[i])
            elif xtype_i in ['video', 'image']:
                h[i] = self.unet_image.middle_block(h[i], emb_image, context[i])
            elif xtype_i == 'text':
                h[i] = self.unet_text.middle_block(h[i], emb_text, context[i])   
            else:
                raise


        for (i_module, t_module, a_module, 
            i_con_in, t_con_in, a_con_in,) \
        in zip(
            self.unet_image.output_blocks, self.unet_text.output_blocks, self.unet_audio.output_blocks, 
            self.unet_image.output_block_connecters_in, self.unet_text.output_block_connecters_in, self.unet_audio.output_block_connecters_in,
            ):
            temp = hs.pop()
            h_connector_out = []
            for i, xtype_i in enumerate(xtype):
                h[i] = th.cat([h[i], temp[i]], dim=1)
                if xtype_i == 'audio':
                    h[i] = a_module(h[i], emb_audio, context[i])
                elif xtype_i in ['video', 'image']:
                    h[i] = i_module(h[i], emb_image, context[i])
                elif xtype_i == 'text':
                    h[i] = t_module(h[i], emb_text, context[i])   
                else:
                    raise

            if i_con_in is not None:
                for i, xtype_i in enumerate(xtype):
                    if xtype_i == 'audio':
                        h[i] = a_con_in(h[i], context=h_con[i])
                    elif xtype_i in ['video', 'image']:
                        h[i] = i_con_in(h[i], context=h_con[i])
                    elif xtype_i == 'text':
                        h[i] = t_con_in(h[i], context=h_con[i])   
                    else:
                        raise

        out_all = []
        for i, xtype_i in enumerate(xtype):
            if xtype_i == 'video':
                num_frames = h[i].shape[2]
                h[i] = rearrange(h[i], 'b c t h w -> (b t) c h w ')
                out = self.unet_image.out(h[i])  
                out = rearrange(out, '(b t) c h w -> b c t h w', t=num_frames)
            elif xtype_i == 'image':
                out = self.unet_image.out(h[i])  
            elif xtype_i == 'text':
                out = self.unet_text.out(h[i]).squeeze(-1).squeeze(-1)
            elif xtype_i == 'audio':
                out = self.unet_audio.out(h[i])
            out_all.append(out)
        return out_all
