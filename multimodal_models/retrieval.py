# -*- coding: utf-8 -*-
"""
Video-text retrieval using CLIP + Mamba pipeline.
Index videos, query by text, return ranked results.
"""
from pathlib import Path
from typing import List, Optional, Tuple, Union

import torch

from .config import MultimodalConfig
from .fusion_model import MultimodalPipeline


def build_index(
    video_paths: List[Union[str, Path]],
    config: Optional[MultimodalConfig] = None,
    device: Optional[str] = None,
) -> Tuple[MultimodalPipeline, torch.Tensor]:
    """
    Encode all videos and return pipeline + embedding matrix (N, D).
    """
    if config is None:
        config = MultimodalConfig()
    if device is not None:
        config.device = device
    pipeline = MultimodalPipeline(config)
    embeddings = pipeline.encode_videos(video_paths)
    return pipeline, embeddings


def search(
    pipeline: MultimodalPipeline,
    embeddings: torch.Tensor,
    video_paths: List[Union[str, Path]],
    query: str,
    top_k: int = 10,
) -> List[Tuple[Union[str, Path], float]]:
    """
    Search indexed videos by text query. Returns list of (path, score).
    """
    text_emb = pipeline.encode_text(query).unsqueeze(0)
    sim = pipeline.similarity_video_text(embeddings, text_emb).squeeze(1)
    scores, indices = torch.topk(sim, min(top_k, len(video_paths)))
    return [(video_paths[i], scores[j].item()) for j, i in enumerate(indices.tolist())]


def run_retrieval_demo(
    video_dir: Union[str, Path],
    query: str = "a person speaking",
    top_k: int = 5,
    config: Optional[MultimodalConfig] = None,
) -> List[Tuple[Union[str, Path], float]]:
    """
    Demo: find videos under video_dir, index them, run one query.
    """
    video_dir = Path(video_dir)
    exts = {".mp4", ".avi", ".mov", ".webm", ".flv", ".mkv"}
    video_paths = [p for p in video_dir.rglob("*") if p.suffix.lower() in exts]
    if not video_paths:
        return []
    pipeline, embeddings = build_index(video_paths, config=config)
    return search(pipeline, embeddings, video_paths, query, top_k=top_k)
