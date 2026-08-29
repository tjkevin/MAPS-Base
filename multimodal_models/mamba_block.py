# -*- coding: utf-8 -*-
"""
Mamba-style sequence block for (batch, seq_len, dim) input.
Pure PyTorch implementation for Windows/CPU compatibility.
For best performance on Linux/CUDA, replace with: from mamba_ssm import Mamba
"""
import math
from typing import Optional

import torch
import torch.nn as nn


class MambaBlockPyTorch(nn.Module):
    """
    Single Mamba-like block: causal sequence modeling over (B, L, D).
    Simplified selective SSM in PyTorch (no CUDA kernel). Accepts same interface as mamba_ssm.Mamba.
    """

    def __init__(
        self,
        d_model: int,
        d_state: int = 16,
        d_conv: int = 4,
        expand: int = 2,
    ):
        super().__init__()
        self.d_model = d_model
        self.d_state = d_state
        self.d_inner = d_model * expand
        self.d_conv = d_conv

        self.in_proj = nn.Linear(d_model, self.d_inner * 2)
        self.conv1d = nn.Conv1d(self.d_inner, self.d_inner, d_conv, padding=d_conv - 1, groups=self.d_inner)
        self.x_proj = nn.Linear(self.d_inner, d_state * 2 + self.d_inner)
        self.out_proj = nn.Linear(self.d_inner, d_model)

        # SSM parameters (simplified: fixed A, learnable B,C, delta)
        self.A_log = nn.Parameter(torch.log(-torch.arange(1, d_state + 1, dtype=torch.float32)))
        self.D = nn.Parameter(torch.ones(self.d_inner))

        self._init_weights()

    def _init_weights(self):
        for m in (self.in_proj, self.x_proj, self.out_proj):
            nn.init.xavier_uniform_(m.weight)
            if m.bias is not None:
                nn.init.zeros_(m.bias)
        nn.init.xavier_uniform_(self.conv1d.weight)
        if self.conv1d.bias is not None:
            nn.init.zeros_(self.conv1d.bias)

    def _ssm_step(self, u, delta, B, C):
        """Single step of discrete SSM: h' = A*h + B*u, y = C*h + D*u (simplified)."""
        batch, length, d_inner = u.shape
        A = -torch.exp(self.A_log.float()).to(u.device)  # (d_state,)
        # Causal: for each t, y_t = sum_s C_t @ A^(t-s) @ B_s @ u_s + D*u_t
        # We use a simple recurrence for clarity (can be parallelized with scan).
        h = torch.zeros(batch, self.d_state, d_inner, device=u.device, dtype=u.dtype)
        out = []
        for t in range(length):
            dt = delta[:, t : t + 1, :]  # (B, 1, d_inner)
            bt = B[:, t : t + 1, :]      # (B, 1, d_state)
            ct = C[:, t : t + 1, :]      # (B, 1, d_state)
            ut = u[:, t : t + 1, :]      # (B, 1, d_inner)
            h = h * torch.exp(A.unsqueeze(0).unsqueeze(-1) * dt) + bt.permute(0, 2, 1) * ut  # (B, d_state, d_inner)
            yt = (ct.permute(0, 2, 1) * h).sum(1) + self.D * ut.squeeze(1)  # (B, d_inner)
            out.append(yt)
        return torch.stack(out, dim=1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        x: (batch, seq_len, d_model)
        return: (batch, seq_len, d_model)
        """
        B, L, D = x.shape
        xz = self.in_proj(x)  # (B, L, 2*d_inner)
        x_, z = xz.chunk(2, dim=-1)
        x_ = x_.permute(0, 2, 1)  # (B, d_inner, L)
        x_ = self.conv1d(x_)[:, :, :L]  # causal: keep only first L
        x_ = x_.permute(0, 2, 1)  # (B, L, d_inner)
        xbc = self.x_proj(x_)  # (B, L, d_state*2 + d_inner)
        delta, B, C = xbc.split([self.d_inner, self.d_state, self.d_state], dim=-1)
        delta = torch.nn.functional.softplus(delta)
        u = x_
        y = self._ssm_step(u, delta, B, C)
        y = y * torch.sigmoid(z)
        return self.out_proj(y)


def get_mamba_block(d_model: int, d_state: int = 16, d_conv: int = 4, expand: int = 2, use_native: bool = False):
    """
    Return a Mamba block. If use_native=True and mamba_ssm is installed (Linux/CUDA), use it.
    Otherwise use PyTorch fallback.
    """
    if use_native:
        try:
            from mamba_ssm import Mamba
            return Mamba(d_model=d_model, d_state=d_state, d_conv=d_conv, expand=expand)
        except ImportError:
            pass
    return MambaBlockPyTorch(d_model=d_model, d_state=d_state, d_conv=d_conv, expand=expand)


class MambaStack(nn.Module):
    """Stack of Mamba blocks with residual and norm for sequence (B, L, D)."""

    def __init__(
        self,
        d_model: int,
        num_layers: int = 2,
        d_state: int = 16,
        d_conv: int = 4,
        expand: int = 2,
        use_native_mamba: bool = False,
    ):
        super().__init__()
        self.layers = nn.ModuleList([
            get_mamba_block(d_model, d_state, d_conv, expand, use_native=use_native_mamba)
            for _ in range(num_layers)
        ])
        self.norms = nn.ModuleList([nn.LayerNorm(d_model) for _ in range(num_layers)])

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        for block, norm in zip(self.layers, self.norms):
            x = x + block(norm(x))
        return x
