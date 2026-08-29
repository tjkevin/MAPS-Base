# -*- coding: utf-8 -*-
"""
CLIP + Mamba fusion: CLIP extracts features, Mamba does cross-modal long-range temporal modeling.
Output: unified video and text embeddings for retrieval / classification.
"""
from pathlib import Path
from typing import List, Optional, Tuple, Union

import torch
import torch.nn as nn
from PIL import Image

from .config import MultimodalConfig
from .clip_extractor import load_clip, encode_images, encode_text, encode_video_frames, video_to_frames
from .mamba_block import MambaStack


class CLIPMambaFusion(nn.Module):
    """
    Feature-level fusion: CLIP (image/frame + text) + Mamba (temporal over frame features).
    - Video: sample frames -> CLIP vision features (T, D) -> Mamba -> pool -> video_embed
    - Text: CLIP text encoder -> text_embed
    Retrieval: similarity(video_embed, text_embed).
    """

    def __init__(self, config: Optional[MultimodalConfig] = None):
        super().__init__()
        self.config = config or MultimodalConfig()
        self._clip_model = None
        self._processor = None
        self._device = None

        # Project CLIP frame features to Mamba dim if needed
        if self.config.mamba_d_model != self.config.clip_projection_dim:
            self.frame_proj = nn.Linear(self.config.clip_projection_dim, self.config.mamba_d_model)
        else:
            self.frame_proj = nn.Identity()

        self.mamba = MambaStack(
            d_model=self.config.mamba_d_model,
            num_layers=self.config.mamba_num_layers,
            d_state=self.config.mamba_d_state,
            d_conv=self.config.mamba_d_conv,
            expand=self.config.mamba_expand,
            use_native_mamba=False,
        )
        self.temporal_norm = nn.LayerNorm(self.config.mamba_d_model)
        self.fusion_dropout = nn.Dropout(self.config.dropout)
        self.video_proj = nn.Linear(self.config.mamba_d_model, self.config.fusion_dim)

    def set_clip(self, model, processor, device: str):
        """Set pre-loaded CLIP model (not owned by this module)."""
        self._clip_model = model
        self._processor = processor
        self._device = device

    def encode_frames_to_sequence(self, frame_features: torch.Tensor) -> torch.Tensor:
        """
        frame_features: (T, D) or (B, T, D) from CLIP. Run Mamba and pool to (D,) or (B, D).
        """
        if frame_features.dim() == 2:
            frame_features = frame_features.unsqueeze(0)
        B, T, D = frame_features.shape
        x = self.frame_proj(frame_features)
        x = self.mamba(x)
        x = self.temporal_norm(x)
        x = self.fusion_dropout(x)
        pooled = x.mean(dim=1)  # (B, mamba_d_model)
        return self.video_proj(pooled)  # (B, fusion_dim)

    def forward_video_tensor(self, frame_features: torch.Tensor) -> torch.Tensor:
        """frame_features: (B, T, D) from CLIP. Return (B, fusion_dim)."""
        return self.encode_frames_to_sequence(frame_features)

    def forward_video_from_path(
        self,
        video_path: Union[str, Path],
        fps: Optional[float] = None,
        max_frames: Optional[int] = None,
    ) -> torch.Tensor:
        """Encode one video file to (1, fusion_dim)."""
        if self._clip_model is None:
            raise RuntimeError("Call set_clip() first or use encode_video() with pipeline.")
        fps = fps or self.config.video_fps
        max_frames = max_frames or self.config.max_frames
        clip_feats = encode_video_frames(
            self._clip_model,
            self._processor,
            video_path,
            self._device,
            fps=fps,
            max_frames=max_frames,
            frame_size=self.config.frame_size,
            normalize=True,
        )
        clip_feats = clip_feats.unsqueeze(0)
        return self.forward_video_tensor(clip_feats)

    def forward_text_from_strings(self, texts: List[str]) -> torch.Tensor:
        """Encode text with CLIP. Returns (B, D) in CLIP space; project to fusion_dim if needed."""
        if self._clip_model is None:
            raise RuntimeError("Call set_clip() first.")
        text_feats = encode_text(
            self._clip_model,
            self._processor,
            texts,
            self._device,
            normalize=True,
        )
        return text_feats


class MultimodalPipeline:
    """
    End-to-end pipeline: load CLIP + fusion model, encode video/text, compute similarity.
    """

    def __init__(self, config: Optional[MultimodalConfig] = None):
        self.config = config or MultimodalConfig()
        self.device = self.config.device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.clip_model, self.processor, _ = load_clip(
            self.config.clip_model_name,
            device=self.device,
        )
        self.fusion = CLIPMambaFusion(self.config).to(self.device)
        self.fusion.set_clip(self.clip_model, self.processor, self.device)
        self.fusion.eval()

    def encode_video(self, video_path: Union[str, Path]) -> torch.Tensor:
        """Encode one video to (fusion_dim,) tensor."""
        with torch.no_grad():
            out = self.fusion.forward_video_from_path(video_path)
        return out.squeeze(0)

    def encode_videos(self, video_paths: List[Union[str, Path]]) -> torch.Tensor:
        """Encode multiple videos. Returns (N, fusion_dim)."""
        with torch.no_grad():
            feats = []
            for p in video_paths:
                feats.append(self.fusion.forward_video_from_path(p))
            return torch.cat(feats, dim=0)

    def encode_text(self, text: str) -> torch.Tensor:
        """Encode one text to (clip_projection_dim,) in CLIP space."""
        with torch.no_grad():
            out = encode_text(
                self.clip_model,
                self.processor,
                [text],
                self.device,
                normalize=True,
            )
        return out.squeeze(0)

    def encode_texts(self, texts: List[str]) -> torch.Tensor:
        """Encode multiple texts. Returns (N, clip_projection_dim)."""
        with torch.no_grad():
            return encode_text(self.clip_model, self.processor, texts, self.device, normalize=True)

    def similarity_video_text(self, video_embed: torch.Tensor, text_embed: torch.Tensor) -> torch.Tensor:
        """Cosine similarity. video_embed: (V, D), text_embed: (T, D) -> (V, T)."""
        v = video_embed / (video_embed.norm(dim=-1, keepdim=True) + 1e-8)
        t = text_embed / (text_embed.norm(dim=-1, keepdim=True) + 1e-8)
        return v @ t.t()

    def retrieve_videos_by_text(
        self,
        video_paths: List[Union[str, Path]],
        query: str,
        top_k: int = 5,
    ) -> List[Tuple[Union[str, Path], float]]:
        """Return top_k (video_path, score) for query text."""
        v_emb = self.encode_videos(video_paths)
        t_emb = self.encode_text(query).unsqueeze(0)
        sim = self.similarity_video_text(v_emb, t_emb).squeeze(1)
        scores, indices = torch.topk(sim, min(top_k, len(video_paths)))
        return [(video_paths[i], scores[j].item()) for j, i in enumerate(indices.tolist())]
