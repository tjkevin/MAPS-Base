# -*- coding: utf-8 -*-
"""
Multimodal models: CLIP (OpenAI) + Mamba (state-spaces) feature-level fusion
for video/text recognition, processing, and retrieval.
"""
from .config import MultimodalConfig
from .clip_extractor import (
    load_clip,
    encode_images,
    encode_text,
    encode_video_frames,
    video_to_frames,
)
from .mamba_block import MambaBlockPyTorch, MambaStack, get_mamba_block
from .fusion_model import CLIPMambaFusion, MultimodalPipeline
from .retrieval import build_index, search, run_retrieval_demo

__all__ = [
    "MultimodalConfig",
    "load_clip",
    "encode_images",
    "encode_text",
    "encode_video_frames",
    "video_to_frames",
    "MambaBlockPyTorch",
    "MambaStack",
    "get_mamba_block",
    "CLIPMambaFusion",
    "MultimodalPipeline",
    "build_index",
    "search",
    "run_retrieval_demo",
]
