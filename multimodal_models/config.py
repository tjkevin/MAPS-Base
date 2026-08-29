# -*- coding: utf-8 -*-
"""
Configuration for CLIP + Mamba multimodal model.
Feature-level fusion: CLIP extracts features, Mamba does cross-modal long-range modeling.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MultimodalConfig:
    """Configuration for CLIP + Mamba fusion."""

    # CLIP (OpenAI-style, via Hugging Face)
    clip_model_name: str = "openai/clip-vit-base-patch32"
    clip_projection_dim: int = 512  # CLIP ViT-B/32 output projection dim

    # Video / frame
    video_fps: float = 1.0  # frames per second to sample from video
    max_frames: int = 32  # max number of frames per video (sequence length for Mamba)
    frame_size: int = 224  # CLIP ViT expects 224

    # Mamba (state-space, sequence over frame features)
    mamba_d_model: int = 512  # should match CLIP projection_dim for direct feed
    mamba_d_state: int = 16
    mamba_d_conv: int = 4
    mamba_expand: int = 2
    mamba_num_layers: int = 2  # stack of Mamba blocks for temporal modeling

    # Fusion / retrieval
    use_text_branch: bool = True  # use CLIP text encoder for text queries
    fusion_dim: int = 512  # output dim for video and text embeddings (for retrieval)
    dropout: float = 0.1

    # Device
    device: Optional[str] = None  # "cuda" or "cpu", None = auto

    def __post_init__(self):
        if self.mamba_d_model != self.clip_projection_dim:
            # Allow projection if different
            self._need_proj = True
        else:
            self._need_proj = False
