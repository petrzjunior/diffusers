# Copyright 2023 The HuggingFace Team. All rights reserved.
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
import math
from typing import Any, Dict, Optional, Tuple, Union

import torch
from torch import nn

from ..utils import is_torch_version, logging
from ..utils.torch_utils import apply_freeu
from .activations import get_activation
from .resnet import Downsample1D, ResidualTemporalBlock1D, ResnetBlock1D, Upsample1D, rearrange_dims


logger = logging.get_logger(__name__)  # pylint: disable=invalid-name


class DownResnetBlock1D(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: Optional[int] = None,
        num_layers: int = 1,
        conv_shortcut: bool = False,
        temb_channels: int = 32,
        groups: int = 32,
        groups_out: Optional[int] = None,
        non_linearity: Optional[str] = None,
        time_embedding_norm: str = "default",
        output_scale_factor: float = 1.0,
        add_downsample: bool = True,
    ):
        super().__init__()
        self.in_channels = in_channels
        out_channels = in_channels if out_channels is None else out_channels
        self.out_channels = out_channels
        self.use_conv_shortcut = conv_shortcut
        self.time_embedding_norm = time_embedding_norm
        self.add_downsample = add_downsample
        self.output_scale_factor = output_scale_factor

        if groups_out is None:
            groups_out = groups

        # there will always be at least one resnet
        resnets = [ResidualTemporalBlock1D(in_channels, out_channels, embed_dim=temb_channels)]

        for _ in range(num_layers):
            resnets.append(ResidualTemporalBlock1D(out_channels, out_channels, embed_dim=temb_channels))

        self.resnets = nn.ModuleList(resnets)

        if non_linearity is None:
            self.nonlinearity = None
        else:
            self.nonlinearity = get_activation(non_linearity)

        self.downsample = None
        if add_downsample:
            self.downsample = Downsample1D(out_channels, use_conv=True, padding=1)

    def forward(self, hidden_states: torch.FloatTensor, temb: Optional[torch.FloatTensor] = None) -> torch.FloatTensor:
        output_states = ()

        hidden_states = self.resnets[0](hidden_states, temb)
        for resnet in self.resnets[1:]:
            hidden_states = resnet(hidden_states, temb)

        output_states += (hidden_states,)

        if self.nonlinearity is not None:
            hidden_states = self.nonlinearity(hidden_states)

        if self.downsample is not None:
            hidden_states = self.downsample(hidden_states)

        return hidden_states, output_states


class UpResnetBlock1D(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: Optional[int] = None,
        num_layers: int = 1,
        temb_channels: int = 32,
        groups: int = 32,
        groups_out: Optional[int] = None,
        non_linearity: Optional[str] = None,
        time_embedding_norm: str = "default",
        output_scale_factor: float = 1.0,
        add_upsample: bool = True,
    ):
        super().__init__()
        self.in_channels = in_channels
        out_channels = in_channels if out_channels is None else out_channels
        self.out_channels = out_channels
        self.time_embedding_norm = time_embedding_norm
        self.add_upsample = add_upsample
        self.output_scale_factor = output_scale_factor

        if groups_out is None:
            groups_out = groups

        # there will always be at least one resnet
        resnets = [ResidualTemporalBlock1D(2 * in_channels, out_channels, embed_dim=temb_channels)]

        for _ in range(num_layers):
            resnets.append(ResidualTemporalBlock1D(out_channels, out_channels, embed_dim=temb_channels))

        self.resnets = nn.ModuleList(resnets)

        if non_linearity is None:
            self.nonlinearity = None
        else:
            self.nonlinearity = get_activation(non_linearity)

        self.upsample = None
        if add_upsample:
            self.upsample = Upsample1D(out_channels, use_conv_transpose=True)

    def forward(
        self,
        hidden_states: torch.FloatTensor,
        res_hidden_states_tuple: Optional[Tuple[torch.FloatTensor, ...]] = None,
        temb: Optional[torch.FloatTensor] = None,
    ) -> torch.FloatTensor:
        if res_hidden_states_tuple is not None:
            res_hidden_states = res_hidden_states_tuple[-1]
            hidden_states = torch.cat((hidden_states, res_hidden_states), dim=1)

        hidden_states = self.resnets[0](hidden_states, temb)
        for resnet in self.resnets[1:]:
            hidden_states = resnet(hidden_states, temb)

        if self.nonlinearity is not None:
            hidden_states = self.nonlinearity(hidden_states)

        if self.upsample is not None:
            hidden_states = self.upsample(hidden_states)

        return hidden_states


class ValueFunctionMidBlock1D(nn.Module):
    def __init__(self, in_channels: int, out_channels: int, embed_dim: int):
        super().__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.embed_dim = embed_dim

        self.res1 = ResidualTemporalBlock1D(in_channels, in_channels // 2, embed_dim=embed_dim)
        self.down1 = Downsample1D(out_channels // 2, use_conv=True)
        self.res2 = ResidualTemporalBlock1D(in_channels // 2, in_channels // 4, embed_dim=embed_dim)
        self.down2 = Downsample1D(out_channels // 4, use_conv=True)

    def forward(self, x: torch.FloatTensor, temb: Optional[torch.FloatTensor] = None) -> torch.FloatTensor:
        x = self.res1(x, temb)
        x = self.down1(x)
        x = self.res2(x, temb)
        x = self.down2(x)
        return x


class MidResTemporalBlock1D(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        embed_dim: int,
        num_layers: int = 1,
        add_downsample: bool = False,
        add_upsample: bool = False,
        non_linearity: Optional[str] = None,
    ):
        super().__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.add_downsample = add_downsample

        # there will always be at least one resnet
        resnets = [ResidualTemporalBlock1D(in_channels, out_channels, embed_dim=embed_dim)]

        for _ in range(num_layers):
            resnets.append(ResidualTemporalBlock1D(out_channels, out_channels, embed_dim=embed_dim))

        self.resnets = nn.ModuleList(resnets)

        if non_linearity is None:
            self.nonlinearity = None
        else:
            self.nonlinearity = get_activation(non_linearity)

        self.upsample = None
        if add_upsample:
            self.upsample = Downsample1D(out_channels, use_conv=True)

        self.downsample = None
        if add_downsample:
            self.downsample = Downsample1D(out_channels, use_conv=True)

        if self.upsample and self.downsample:
            raise ValueError("Block cannot downsample and upsample")

    def forward(self, hidden_states: torch.FloatTensor, temb: torch.FloatTensor) -> torch.FloatTensor:
        hidden_states = self.resnets[0](hidden_states, temb)
        for resnet in self.resnets[1:]:
            hidden_states = resnet(hidden_states, temb)

        if self.upsample:
            hidden_states = self.upsample(hidden_states)
        if self.downsample:
            self.downsample = self.downsample(hidden_states)

        return hidden_states


class OutConv1DBlock(nn.Module):
    def __init__(self, in_channels: int, out_channels: int, num_groups_out: int, embed_dim: int, act_fn: str):
        super().__init__()
        self.final_conv1d_1 = nn.Conv1d(in_channels, in_channels, 5, padding=2)
        self.final_conv1d_gn = nn.GroupNorm(num_groups_out, in_channels)
        self.final_conv1d_act = get_activation(act_fn)
        self.final_conv1d_2 = nn.Conv1d(in_channels, out_channels, 1)

    def forward(self, hidden_states: torch.FloatTensor, temb: Optional[torch.FloatTensor] = None) -> torch.FloatTensor:
        hidden_states = self.final_conv1d_1(hidden_states)
        hidden_states = rearrange_dims(hidden_states)
        hidden_states = self.final_conv1d_gn(hidden_states)
        hidden_states = rearrange_dims(hidden_states)
        hidden_states = self.final_conv1d_act(hidden_states)
        hidden_states = self.final_conv1d_2(hidden_states)
        return hidden_states


class OutValueFunctionBlock(nn.Module):
    def __init__(self, fc_dim: int, embed_dim: int, act_fn: str = "mish"):
        super().__init__()
        self.final_block = nn.ModuleList(
            [
                nn.Linear(fc_dim + embed_dim, fc_dim // 2),
                get_activation(act_fn),
                nn.Linear(fc_dim // 2, 1),
            ]
        )

    def forward(self, hidden_states: torch.FloatTensor, temb: torch.FloatTensor) -> torch.FloatTensor:
        hidden_states = hidden_states.view(hidden_states.shape[0], -1)
        hidden_states = torch.cat((hidden_states, temb), dim=-1)
        for layer in self.final_block:
            hidden_states = layer(hidden_states)

        return hidden_states


_kernels = {
    "linear": [1 / 8, 3 / 8, 3 / 8, 1 / 8],
    "cubic": [-0.01171875, -0.03515625, 0.11328125, 0.43359375, 0.43359375, 0.11328125, -0.03515625, -0.01171875],
    "lanczos3": [
        0.003689131001010537,
        0.015056144446134567,
        -0.03399861603975296,
        -0.066637322306633,
        0.13550527393817902,
        0.44638532400131226,
        0.44638532400131226,
        0.13550527393817902,
        -0.066637322306633,
        -0.03399861603975296,
        0.015056144446134567,
        0.003689131001010537,
    ],
}


class SelfAttention1d(nn.Module):
    def __init__(self, in_channels: int, n_head: int = 1, dropout_rate: float = 0.0):
        super().__init__()
        self.channels = in_channels
        self.group_norm = nn.GroupNorm(1, num_channels=in_channels)
        self.num_heads = n_head

        self.query = nn.Linear(self.channels, self.channels)
        self.key = nn.Linear(self.channels, self.channels)
        self.value = nn.Linear(self.channels, self.channels)

        self.proj_attn = nn.Linear(self.channels, self.channels, bias=True)

        self.dropout = nn.Dropout(dropout_rate, inplace=True)

    def transpose_for_scores(self, projection: torch.Tensor) -> torch.Tensor:
        new_projection_shape = projection.size()[:-1] + (self.num_heads, -1)
        # move heads to 2nd position (B, T, H * D) -> (B, T, H, D) -> (B, H, T, D)
        new_projection = projection.view(new_projection_shape).permute(0, 2, 1, 3)
        return new_projection

    def forward(self, hidden_states: torch.FloatTensor) -> torch.FloatTensor:
        residual = hidden_states
        batch, channel_dim, seq = hidden_states.shape

        hidden_states = self.group_norm(hidden_states)
        hidden_states = hidden_states.transpose(1, 2)

        query_proj = self.query(hidden_states)
        key_proj = self.key(hidden_states)
        value_proj = self.value(hidden_states)

        query_states = self.transpose_for_scores(query_proj)
        key_states = self.transpose_for_scores(key_proj)
        value_states = self.transpose_for_scores(value_proj)

        scale = 1 / math.sqrt(math.sqrt(key_states.shape[-1]))

        attention_scores = torch.matmul(query_states * scale, key_states.transpose(-1, -2) * scale)
        attention_probs = torch.softmax(attention_scores, dim=-1)

        # compute attention output
        hidden_states = torch.matmul(attention_probs, value_states)

        hidden_states = hidden_states.permute(0, 2, 1, 3).contiguous()
        new_hidden_states_shape = hidden_states.size()[:-2] + (self.channels,)
        hidden_states = hidden_states.view(new_hidden_states_shape)

        # compute next hidden_states
        hidden_states = self.proj_attn(hidden_states)
        hidden_states = hidden_states.transpose(1, 2)
        hidden_states = self.dropout(hidden_states)

        output = hidden_states + residual

        return output


class ResConvBlock(nn.Module):
    def __init__(self, in_channels: int, mid_channels: int, out_channels: int, is_last: bool = False):
        super().__init__()
        self.is_last = is_last
        self.has_conv_skip = in_channels != out_channels

        if self.has_conv_skip:
            self.conv_skip = nn.Conv1d(in_channels, out_channels, 1, bias=False)

        self.conv_1 = nn.Conv1d(in_channels, mid_channels, 5, padding=2)
        self.group_norm_1 = nn.GroupNorm(1, mid_channels)
        self.gelu_1 = nn.GELU()
        self.conv_2 = nn.Conv1d(mid_channels, out_channels, 5, padding=2)

        if not self.is_last:
            self.group_norm_2 = nn.GroupNorm(1, out_channels)
            self.gelu_2 = nn.GELU()

    def forward(self, hidden_states: torch.FloatTensor) -> torch.FloatTensor:
        residual = self.conv_skip(hidden_states) if self.has_conv_skip else hidden_states

        hidden_states = self.conv_1(hidden_states)
        hidden_states = self.group_norm_1(hidden_states)
        hidden_states = self.gelu_1(hidden_states)
        hidden_states = self.conv_2(hidden_states)

        if not self.is_last:
            hidden_states = self.group_norm_2(hidden_states)
            hidden_states = self.gelu_2(hidden_states)

        output = hidden_states + residual
        return output


class UNetMidBlock1D(nn.Module):
    def __init__(
        self,
        in_channels: int,
        temb_channels: int,
        dropout: float = 0.0,
        num_layers: int = 1,
        resnet_eps: float = 1e-6,
        resnet_time_scale_shift: str = "default",  # default, spatial
        resnet_act_fn: str = "swish",
        resnet_groups: int = 32,
        attn_groups: Optional[int] = None,
        resnet_pre_norm: bool = True,
        add_attention: bool = True,
        attention_head_dim: int = 1,
        output_scale_factor: float = 1.0,
    ):
        super().__init__()

        resnet_groups = resnet_groups if resnet_groups is not None else min(in_channels // 4, 32)
        self.add_attention = add_attention

        if attn_groups is None:
            attn_groups = resnet_groups if resnet_time_scale_shift == "default" else None

        # there is always at least one resnet
        resnets = [
            ResnetBlock1D(
                in_channels=in_channels,
                out_channels=in_channels,
                temb_channels=temb_channels,
                eps=resnet_eps,
                groups=resnet_groups,
                dropout=dropout,
                time_embedding_norm=resnet_time_scale_shift,
                non_linearity=resnet_act_fn,
                output_scale_factor=output_scale_factor,
                pre_norm=resnet_pre_norm,
            )
        ]
        attentions = []

        if attention_head_dim is None:
            logger.warn(
                f"It is not recommend to pass `attention_head_dim=None`. Defaulting `attention_head_dim` to `in_channels`: {in_channels}."
            )
            attention_head_dim = in_channels

        for _ in range(num_layers):
            if self.add_attention:
                attentions.append(
                    SelfAttention1d(in_channels, n_head=in_channels // attention_head_dim, dropout_rate=dropout)
                )
            else:
                attentions.append(None)

            resnets.append(
                ResnetBlock1D(
                    in_channels=in_channels,
                    out_channels=in_channels,
                    temb_channels=temb_channels,
                    eps=resnet_eps,
                    groups=resnet_groups,
                    dropout=dropout,
                    time_embedding_norm=resnet_time_scale_shift,
                    non_linearity=resnet_act_fn,
                    output_scale_factor=output_scale_factor,
                    pre_norm=resnet_pre_norm,
                )
            )

        self.attentions = nn.ModuleList(attentions)
        self.resnets = nn.ModuleList(resnets)

    def forward(self, hidden_states: torch.FloatTensor, temb: Optional[torch.FloatTensor] = None) -> torch.FloatTensor:
        hidden_states = self.resnets[0](hidden_states, temb)
        for attn, resnet in zip(self.attentions, self.resnets[1:]):
            if attn is not None:
                hidden_states = attn(hidden_states)  # add temb when attention supports it
            hidden_states = resnet(hidden_states, temb)

        return hidden_states


class AttnDownBlock1D(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        temb_channels: int,
        dropout: float = 0.0,
        num_layers: int = 1,
        resnet_eps: float = 1e-6,
        resnet_time_scale_shift: str = "default",
        resnet_act_fn: str = "swish",
        resnet_groups: int = 32,
        resnet_pre_norm: bool = True,
        attention_head_dim: int = 1,
        output_scale_factor: float = 1.0,
        downsample_padding: int = 1,
        downsample_type: str = "conv",
    ):
        super().__init__()
        resnets = []
        attentions = []
        self.downsample_type = downsample_type

        if attention_head_dim is None:
            logger.warn(
                f"It is not recommend to pass `attention_head_dim=None`. Defaulting `attention_head_dim` to `in_channels`: {out_channels}."
            )
            attention_head_dim = out_channels

        for i in range(num_layers):
            in_channels = in_channels if i == 0 else out_channels
            resnets.append(
                ResnetBlock1D(
                    in_channels=in_channels,
                    out_channels=out_channels,
                    temb_channels=temb_channels,
                    eps=resnet_eps,
                    groups=resnet_groups,
                    dropout=dropout,
                    time_embedding_norm=resnet_time_scale_shift,
                    non_linearity=resnet_act_fn,
                    output_scale_factor=output_scale_factor,
                    pre_norm=resnet_pre_norm,
                )
            )
            attentions.append(
                SelfAttention1d(
                    out_channels,
                    heads=out_channels // attention_head_dim,
                    dim_head=attention_head_dim,
                    rescale_output_factor=output_scale_factor,
                    eps=resnet_eps,
                    norm_num_groups=resnet_groups,
                    residual_connection=True,
                    bias=True,
                    upcast_softmax=True,
                    _from_deprecated_attn_block=True,
                )
            )

        self.attentions = nn.ModuleList(attentions)
        self.resnets = nn.ModuleList(resnets)

        if downsample_type == "conv":
            self.downsamplers = nn.ModuleList(
                [Downsample1D(out_channels, use_conv=True, out_channels=out_channels, padding=downsample_padding)]
            )
        elif downsample_type == "resnet":
            self.downsamplers = nn.ModuleList(
                [
                    ResnetBlock1D(
                        in_channels=out_channels,
                        out_channels=out_channels,
                        temb_channels=temb_channels,
                        eps=resnet_eps,
                        groups=resnet_groups,
                        dropout=dropout,
                        time_embedding_norm=resnet_time_scale_shift,
                        non_linearity=resnet_act_fn,
                        output_scale_factor=output_scale_factor,
                        pre_norm=resnet_pre_norm,
                        down=True,
                    )
                ]
            )
        else:
            self.downsamplers = None

    def forward(
        self,
        hidden_states: torch.FloatTensor,
        temb: Optional[torch.FloatTensor] = None,
        upsample_size: Optional[int] = None,
        cross_attention_kwargs: Optional[Dict[str, Any]] = None,
    ) -> Tuple[torch.FloatTensor, Tuple[torch.FloatTensor, ...]]:
        cross_attention_kwargs = cross_attention_kwargs if cross_attention_kwargs is not None else {}

        lora_scale = cross_attention_kwargs.get("scale", 1.0)

        output_states = ()

        for resnet, attn in zip(self.resnets, self.attentions):
            cross_attention_kwargs.update({"scale": lora_scale})
            hidden_states = resnet(hidden_states, temb, scale=lora_scale)
            hidden_states = attn(hidden_states, **cross_attention_kwargs)
            output_states = output_states + (hidden_states,)

        if self.downsamplers is not None:
            for downsampler in self.downsamplers:
                if self.downsample_type == "resnet":
                    hidden_states = downsampler(hidden_states, temb=temb, scale=lora_scale)
                else:
                    hidden_states = downsampler(hidden_states, scale=lora_scale)

            output_states += (hidden_states,)

        return hidden_states, output_states


class DownBlock1D(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        temb_channels: int,
        dropout: float = 0.0,
        num_layers: int = 3,
        resnet_eps: float = 1e-6,
        resnet_time_scale_shift: str = "default",
        resnet_act_fn: str = "swish",
        resnet_groups: int = 32,
        resnet_pre_norm: bool = True,
        output_scale_factor: float = 1.0,
        add_downsample: bool = True,
        downsample_padding: int = 1,
    ):
        super().__init__()
        resnets = []

        for i in range(num_layers):
            in_channels = in_channels if i == 0 else out_channels
            resnets.append(
                ResnetBlock1D(
                    in_channels=in_channels,
                    out_channels=out_channels,
                    temb_channels=temb_channels,
                    eps=resnet_eps,
                    groups=resnet_groups,
                    dropout=dropout,
                    time_embedding_norm=resnet_time_scale_shift,
                    non_linearity=resnet_act_fn,
                    output_scale_factor=output_scale_factor,
                    pre_norm=resnet_pre_norm,
                )
            )

        self.resnets = nn.ModuleList(resnets)

        if add_downsample:
            self.downsamplers = nn.ModuleList(
                [
                    Downsample1D(
                        out_channels,
                        use_conv=True,
                        out_channels=out_channels,
                        padding=downsample_padding,
                    )
                ]
            )
        else:
            self.downsamplers = None

        self.gradient_checkpointing = False

    def forward(
        self, hidden_states: torch.FloatTensor, temb: Optional[torch.FloatTensor] = None, scale: float = 1.0
    ) -> Tuple[torch.FloatTensor, Tuple[torch.FloatTensor, ...]]:
        output_states = ()

        for resnet in self.resnets:
            if self.training and self.gradient_checkpointing:

                def create_custom_forward(module):
                    def custom_forward(*inputs):
                        return module(*inputs)

                    return custom_forward

                if is_torch_version(">=", "1.11.0"):
                    hidden_states = torch.utils.checkpoint.checkpoint(
                        create_custom_forward(resnet), hidden_states, temb, use_reentrant=False
                    )
                else:
                    hidden_states = torch.utils.checkpoint.checkpoint(
                        create_custom_forward(resnet), hidden_states, temb
                    )
            else:
                hidden_states = resnet(hidden_states, temb, scale=scale)

            output_states = output_states + (hidden_states,)

        if self.downsamplers is not None:
            for downsampler in self.downsamplers:
                hidden_states = downsampler(hidden_states, scale=scale)

        return hidden_states, output_states


class DownBlock1DNoSkip(nn.Module):
    def __init__(self, out_channels: int, in_channels: int, mid_channels: Optional[int] = None):
        super().__init__()
        mid_channels = out_channels if mid_channels is None else mid_channels

        resnets = [
            ResConvBlock(in_channels, mid_channels, mid_channels),
            ResConvBlock(mid_channels, mid_channels, mid_channels),
            ResConvBlock(mid_channels, mid_channels, out_channels),
        ]

        self.resnets = nn.ModuleList(resnets)

    def forward(self, hidden_states: torch.FloatTensor, temb: Optional[torch.FloatTensor] = None) -> torch.FloatTensor:
        hidden_states = torch.cat([hidden_states, temb], dim=1)
        for resnet in self.resnets:
            hidden_states = resnet(hidden_states)

        return hidden_states, (hidden_states,)


class AttnUpBlock1D(nn.Module):
    def __init__(
        self,
        in_channels: int,
        skip_channels: int,
        out_channels: int,
        temb_channels: int,
        resolution_idx: int = None,
        dropout: float = 0.0,
        num_layers: int = 1,
        resnet_eps: float = 1e-6,
        resnet_time_scale_shift: str = "default",
        resnet_act_fn: str = "swish",
        resnet_groups: int = 32,
        resnet_pre_norm: bool = True,
        attention_head_dim: int = 1,
        output_scale_factor: float = 1.0,
        upsample_type: str = "conv",
    ):
        super().__init__()
        resnets = []
        attentions = []

        self.upsample_type = upsample_type

        if attention_head_dim is None:
            logger.warn(
                f"It is not recommend to pass `attention_head_dim=None`. Defaulting `attention_head_dim` to `in_channels`: {out_channels}."
            )
            attention_head_dim = out_channels

        for i in range(num_layers):
            res_skip_channels = skip_channels if (i == num_layers - 1) else out_channels
            resnet_in_channels = in_channels if i == 0 else out_channels

            resnets.append(
                ResnetBlock1D(
                    in_channels=resnet_in_channels + res_skip_channels,
                    out_channels=out_channels,
                    temb_channels=temb_channels,
                    eps=resnet_eps,
                    groups=resnet_groups,
                    dropout=dropout,
                    time_embedding_norm=resnet_time_scale_shift,
                    non_linearity=resnet_act_fn,
                    output_scale_factor=output_scale_factor,
                    pre_norm=resnet_pre_norm,
                )
            )
            attentions.append(
                SelfAttention1d(
                    out_channels,
                    n_head=out_channels // attention_head_dim,
                )
            )

        self.attentions = nn.ModuleList(attentions)
        self.resnets = nn.ModuleList(resnets)

        if upsample_type == "conv":
            self.upsamplers = nn.ModuleList([Upsample1D(out_channels, use_conv=True, out_channels=out_channels)])
        elif upsample_type == "resnet":
            self.upsamplers = nn.ModuleList(
                [
                    ResnetBlock1D(
                        in_channels=out_channels,
                        out_channels=out_channels,
                        temb_channels=temb_channels,
                        eps=resnet_eps,
                        groups=resnet_groups,
                        dropout=dropout,
                        time_embedding_norm=resnet_time_scale_shift,
                        non_linearity=resnet_act_fn,
                        output_scale_factor=output_scale_factor,
                        pre_norm=resnet_pre_norm,
                        up=True,
                    )
                ]
            )
        else:
            self.upsamplers = None

        self.resolution_idx = resolution_idx

    def forward(
        self,
        hidden_states: torch.FloatTensor,
        res_hidden_states_tuple: Tuple[torch.FloatTensor, ...],
        temb: Optional[torch.FloatTensor] = None,
        upsample_size: Optional[int] = None,
        scale: float = 1.0,
    ) -> torch.FloatTensor:
        for resnet, attn in zip(self.resnets, self.attentions):
            # pop res hidden states
            res_hidden_states = res_hidden_states_tuple[-1]
            res_hidden_states_tuple = res_hidden_states_tuple[:-1]
            hidden_states = torch.cat([hidden_states, res_hidden_states], dim=1)

            hidden_states = resnet(hidden_states, temb, scale=scale)
            cross_attention_kwargs = {"scale": scale}
            hidden_states = attn(hidden_states, **cross_attention_kwargs)

        if self.upsamplers is not None:
            for upsampler in self.upsamplers:
                if self.upsample_type == "resnet":
                    hidden_states = upsampler(hidden_states, temb=temb, scale=scale)
                else:
                    hidden_states = upsampler(hidden_states, scale=scale)

        return hidden_states


class UpBlock1D(nn.Module):
    def __init__(
        self,
        in_channels: int,
        skip_channels: int,
        out_channels: int,
        temb_channels: int,
        resolution_idx: int = None,
        dropout: float = 0.0,
        num_layers: int = 3,
        resnet_eps: float = 1e-6,
        resnet_time_scale_shift: str = "default",
        resnet_act_fn: str = "gelu",
        resnet_groups: int = 1,
        resnet_pre_norm: bool = True,
        output_scale_factor: float = 1.0,
        add_upsample: bool = True,
    ):
        super().__init__()
        resnets = []

        for i in range(num_layers):
            res_skip_channels = skip_channels
            resnet_in_channels = in_channels if i == 0 else out_channels

            resnets.append(
                ResnetBlock1D(
                    in_channels=resnet_in_channels + res_skip_channels,
                    out_channels=out_channels,
                    temb_channels=temb_channels,
                    groups=resnet_groups,
                    eps=resnet_eps,
                    dropout=dropout,
                    time_embedding_norm=resnet_time_scale_shift,
                    non_linearity=resnet_act_fn,
                    output_scale_factor=output_scale_factor,
                    pre_norm=resnet_pre_norm,
                )
            )

        self.resnets = nn.ModuleList(resnets)

        if add_upsample:
            self.upsamplers = nn.ModuleList([Upsample1D(out_channels, use_conv=True, out_channels=out_channels)])
        else:
            self.upsamplers = None

        self.gradient_checkpointing = False
        self.resolution_idx = resolution_idx

    def forward(
        self,
        hidden_states: torch.FloatTensor,
        res_hidden_states_tuple: Tuple[torch.FloatTensor, ...],
        temb: Optional[torch.FloatTensor] = None,
        upsample_size: Optional[int] = None,
        scale: float = 1.0,
    ):
        is_freeu_enabled = (
            getattr(self, "s1", None)
            and getattr(self, "s2", None)
            and getattr(self, "b1", None)
            and getattr(self, "b2", None)
        )

        for resnet in self.resnets:
            # pop res hidden states
            res_hidden_states = res_hidden_states_tuple[-1]
            res_hidden_states_tuple = res_hidden_states_tuple[:-1]

            # FreeU: Only operate on the first two stages
            if is_freeu_enabled:
                hidden_states, res_hidden_states = apply_freeu(
                    self.resolution_idx,
                    hidden_states,
                    res_hidden_states,
                    s1=self.s1,
                    s2=self.s2,
                    b1=self.b1,
                    b2=self.b2,
                )

            hidden_states = torch.cat([hidden_states, res_hidden_states], dim=1)

            if self.training and self.gradient_checkpointing:

                def create_custom_forward(module):
                    def custom_forward(*inputs):
                        return module(*inputs)

                    return custom_forward

                if is_torch_version(">=", "1.11.0"):
                    hidden_states = torch.utils.checkpoint.checkpoint(
                        create_custom_forward(resnet), hidden_states, temb, use_reentrant=False
                    )
                else:
                    hidden_states = torch.utils.checkpoint.checkpoint(
                        create_custom_forward(resnet), hidden_states, temb
                    )
            else:
                hidden_states = resnet(hidden_states, temb, scale=scale)

        if self.upsamplers is not None:
            for upsampler in self.upsamplers:
                hidden_states = upsampler(hidden_states, upsample_size, scale=scale)

        return hidden_states


class UpBlock1DNoSkip(nn.Module):
    def __init__(
        self,
        in_channels: int,
        skip_channels: int,
        out_channels: int,
        resolution_idx: int = None,
        dropout: float = 0.0,
        num_layers: int = 3,
        resnet_eps: float = 1e-6,
        resnet_act_fn: str = "gelu",
        resnet_groups: int = 1,
        output_scale_factor: float = 1.0,
    ):
        super().__init__()
        resnets = []

        for i in range(num_layers):
            is_last = i == num_layers - 1
            res_skip_channels = skip_channels if (i == num_layers - 1) else out_channels
            resnet_in_channels = in_channels if i == 0 else out_channels

            resnets.append(
                ResnetBlock1D(
                    in_channels=resnet_in_channels + res_skip_channels,
                    out_channels=out_channels,
                    dropout=dropout,
                    groups=resnet_groups,
                    eps=resnet_eps,
                    non_linearity=resnet_act_fn,
                    output_scale_factor=output_scale_factor,
                    is_last=is_last,
                )
            )

        self.resnets = nn.ModuleList(resnets)

        self.gradient_checkpointing = False
        self.resolution_idx = resolution_idx

    def forward(self, hidden_states, res_hidden_states_tuple, temb=None, upsample_size=None, scale: float = 1.0):
        is_freeu_enabled = (
            getattr(self, "s1", None)
            and getattr(self, "s2", None)
            and getattr(self, "b1", None)
            and getattr(self, "b2", None)
        )

        for resnet in self.resnets:
            # pop res hidden states
            res_hidden_states = res_hidden_states_tuple[-1]
            res_hidden_states_tuple = res_hidden_states_tuple[:-1]

            # FreeU: Only operate on the first two stages
            if is_freeu_enabled:
                hidden_states, res_hidden_states = apply_freeu(
                    self.resolution_idx,
                    hidden_states,
                    res_hidden_states,
                    s1=self.s1,
                    s2=self.s2,
                    b1=self.b1,
                    b2=self.b2,
                )

            hidden_states = torch.cat([hidden_states, res_hidden_states], dim=1)

            if self.training and self.gradient_checkpointing:

                def create_custom_forward(module):
                    def custom_forward(*inputs):
                        return module(*inputs)

                    return custom_forward

                if is_torch_version(">=", "1.11.0"):
                    hidden_states = torch.utils.checkpoint.checkpoint(
                        create_custom_forward(resnet), hidden_states, temb, use_reentrant=False
                    )
                else:
                    hidden_states = torch.utils.checkpoint.checkpoint(
                        create_custom_forward(resnet), hidden_states, temb
                    )
            else:
                hidden_states = resnet(hidden_states, temb, scale=scale)

        if self.upsamplers is not None:
            for upsampler in self.upsamplers:
                hidden_states = upsampler(hidden_states, upsample_size, scale=scale)

        return hidden_states


DownBlockType = Union[DownResnetBlock1D, DownBlock1D, AttnDownBlock1D, DownBlock1DNoSkip]
MidBlockType = Union[MidResTemporalBlock1D, ValueFunctionMidBlock1D, UNetMidBlock1D]
OutBlockType = Union[OutConv1DBlock, OutValueFunctionBlock]
UpBlockType = Union[UpResnetBlock1D, UpBlock1D, AttnUpBlock1D, UpBlock1DNoSkip]


def get_down_block(
    down_block_type: str,
    num_layers: int,
    in_channels: int,
    out_channels: int,
    temb_channels: int,
    add_downsample: bool,
    resnet_eps: float,
    resnet_act_fn: str,
    transformer_layers_per_block: int = 1,
    num_attention_heads: Optional[int] = None,
    resnet_groups: Optional[int] = None,
    cross_attention_dim: Optional[int] = None,
    downsample_padding: Optional[int] = None,
    dual_cross_attention: bool = False,
    use_linear_projection: bool = False,
    only_cross_attention: bool = False,
    upcast_attention: bool = False,
    resnet_time_scale_shift: str = "default",
    attention_type: str = "default",
    resnet_skip_time_act: bool = False,
    resnet_out_scale_factor: float = 1.0,
    cross_attention_norm: Optional[str] = None,
    attention_head_dim: Optional[int] = None,
    downsample_type: Optional[str] = None,
    dropout: float = 0.0,
) -> DownBlockType:
    if down_block_type == "DownResnetBlock1D":
        return DownResnetBlock1D(
            in_channels=in_channels,
            num_layers=num_layers,
            out_channels=out_channels,
            temb_channels=temb_channels,
            add_downsample=add_downsample,
        )
    elif down_block_type == "DownBlock1D":
        return DownBlock1D(
            num_layers=num_layers,
            in_channels=in_channels,
            out_channels=out_channels,
            temb_channels=temb_channels,
            dropout=dropout,
            add_downsample=add_downsample,
            resnet_eps=resnet_eps,
            resnet_act_fn=resnet_act_fn,
            resnet_groups=resnet_groups,
            downsample_padding=downsample_padding,
            resnet_time_scale_shift=resnet_time_scale_shift,
        )
    elif down_block_type == "AttnDownBlock1D":
        if add_downsample is False:
            downsample_type = None
        else:
            downsample_type = downsample_type or "conv"  # default to 'conv'
        return AttnDownBlock1D(
            num_layers=num_layers,
            in_channels=in_channels,
            out_channels=out_channels,
            temb_channels=temb_channels,
            dropout=dropout,
            resnet_eps=resnet_eps,
            resnet_act_fn=resnet_act_fn,
            resnet_groups=resnet_groups,
            downsample_padding=downsample_padding,
            attention_head_dim=attention_head_dim,
            resnet_time_scale_shift=resnet_time_scale_shift,
            downsample_type=downsample_type,
        )
    elif down_block_type == "DownBlock1DNoSkip":
        return DownBlock1DNoSkip(out_channels=out_channels, in_channels=in_channels)
    raise ValueError(f"{down_block_type} does not exist.")


def get_up_block(
    up_block_type: str,
    num_layers: int,
    in_channels: int,
    out_channels: int,
    skip_channels: int,
    temb_channels: int,
    add_upsample: bool,
    resnet_eps: float,
    resnet_act_fn: str = "gelu",
    resolution_idx: Optional[int] = None,
    transformer_layers_per_block: int = 1,
    num_attention_heads: Optional[int] = None,
    resnet_groups: Optional[int] = None,
    cross_attention_dim: Optional[int] = None,
    dual_cross_attention: bool = False,
    use_linear_projection: bool = False,
    only_cross_attention: bool = False,
    upcast_attention: bool = False,
    resnet_time_scale_shift: str = "default",
    attention_type: str = "default",
    resnet_skip_time_act: bool = False,
    resnet_out_scale_factor: float = 1.0,
    cross_attention_norm: Optional[str] = None,
    attention_head_dim: Optional[int] = None,
    upsample_type: Optional[str] = None,
    dropout: float = 0.0,
):
    # If attn head dim is not defined, we default it to the number of heads
    if attention_head_dim is None:
        logger.warn(
            f"It is recommended to provide `attention_head_dim` when calling `get_up_block`. Defaulting `attention_head_dim` to {num_attention_heads}."
        )
        attention_head_dim = num_attention_heads

    if up_block_type == "UpResnetBlock1D":
        return UpResnetBlock1D(
            in_channels=in_channels,
            num_layers=num_layers,
            out_channels=out_channels,
            temb_channels=temb_channels,
            add_upsample=add_upsample,
        )
    elif up_block_type == "UpBlock1D":
        return UpBlock1D(
            num_layers=num_layers,
            in_channels=in_channels,
            skip_channels=skip_channels,
            out_channels=out_channels,
            temb_channels=temb_channels,
            resolution_idx=resolution_idx,
            dropout=dropout,
            add_upsample=add_upsample,
            resnet_eps=resnet_eps,
            resnet_act_fn=resnet_act_fn,
            resnet_groups=resnet_groups,
            resnet_time_scale_shift=resnet_time_scale_shift,
        )
    elif up_block_type == "AttnUpBlock1D":
        if add_upsample is False:
            upsample_type = None
        else:
            upsample_type = upsample_type or "conv"  # default to 'conv'
        return AttnUpBlock1D(
            num_layers=num_layers,
            in_channels=in_channels,
            skip_channels=skip_channels,
            out_channels=out_channels,
            temb_channels=temb_channels,
            resolution_idx=resolution_idx,
            dropout=dropout,
            resnet_eps=resnet_eps,
            resnet_act_fn=resnet_act_fn,
            resnet_groups=resnet_groups,
            attention_head_dim=attention_head_dim,
            resnet_time_scale_shift=resnet_time_scale_shift,
            upsample_type=upsample_type,
        )
    elif up_block_type == "UpBlock1DNoSkip":
        return UpBlock1DNoSkip(
            num_layers=num_layers,
            in_channels=in_channels,
            skip_channels=skip_channels,
            out_channels=out_channels,
            resolution_idx=resolution_idx,
            dropout=dropout,
            add_upsample=add_upsample,
            resnet_eps=resnet_eps,
            resnet_act_fn=resnet_act_fn,
            resnet_groups=resnet_groups,
        )
    raise ValueError(f"{up_block_type} does not exist.")


def get_mid_block(
    mid_block_type: str,
    num_layers: int,
    in_channels: int,
    out_channels: int,
    embed_dim: int,
    add_downsample: bool,
    mid_block_scale_factor: float = 1,
    dropout: float = 0.0,
    act_fn: str = "gelu",
    attention_head_dim: Optional[int] = 8,
    norm_num_groups: int = 32,
    attn_norm_num_groups: Optional[int] = None,
    norm_eps: float = 1e-5,
    resnet_time_scale_shift: str = "default",
    add_attention: bool = True,
) -> MidBlockType:
    if mid_block_type == "MidResTemporalBlock1D":
        return MidResTemporalBlock1D(
            num_layers=num_layers,
            in_channels=in_channels,
            out_channels=out_channels,
            embed_dim=embed_dim,
            add_downsample=add_downsample,
        )
    elif mid_block_type == "ValueFunctionMidBlock1D":
        return ValueFunctionMidBlock1D(in_channels=in_channels, out_channels=out_channels, embed_dim=embed_dim)
    elif mid_block_type == "UNetMidBlock1D":
        return UNetMidBlock1D(
            in_channels=in_channels,
            temb_channels=embed_dim,
            dropout=dropout,
            resnet_eps=norm_eps,
            resnet_act_fn=act_fn,
            output_scale_factor=mid_block_scale_factor,
            resnet_time_scale_shift=resnet_time_scale_shift,
            attention_head_dim=attention_head_dim,
            resnet_groups=norm_num_groups,
            attn_groups=attn_norm_num_groups,
            add_attention=add_attention,
        )
    raise ValueError(f"{mid_block_type} does not exist.")


def get_out_block(
    *,
    out_block_type: str,
    in_channels: int,
    out_channels: int,
    num_groups_out: int,
    embed_dim: int,
    act_fn: str,
    fc_dim: int,
) -> Optional[OutBlockType]:
    if out_block_type == "OutConv1DBlock":
        return OutConv1DBlock(
            in_channels=in_channels,
            out_channels=out_channels,
            num_groups_out=num_groups_out,
            embed_dim=embed_dim,
            act_fn=act_fn,
        )
    elif out_block_type == "ValueFunction":
        return OutValueFunctionBlock(fc_dim, embed_dim, act_fn)
    return None
