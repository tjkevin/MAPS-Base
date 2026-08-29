# -*- coding: utf-8 -*-
"""
CLIP feature extraction for images and video frames.
Uses Hugging Face Transformers (OpenAI CLIP) for compatibility and no CUDA-only deps.
"""
from pathlib import Path
from typing import List, Optional, Union

import torch
from PIL import Image
from torch import nn

# Optional: video loading
try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False


def _get_clip():
    from transformers import AutoProcessor, CLIPModel
    return CLIPModel, AutoProcessor


def load_clip(model_name: str = "openai/clip-vit-base-patch32", device: Optional[str] = None):
    """Load CLIP model and processor."""
    CLIPModel, AutoProcessor = _get_clip()
    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    model = CLIPModel.from_pretrained(model_name)
    processor = AutoProcessor.from_pretrained(model_name)
    model = model.to(device)
    model.eval()
    return model, processor, device


def encode_images(
    model,
    processor,
    images: List[Image.Image],
    device: str,
    normalize: bool = True,
) -> torch.Tensor:
    """Encode a list of PIL images to CLIP vision features. Returns (N, D)."""
    inputs = processor(images=images, return_tensors="pt", padding=True)
    inputs = {k: v.to(device) for k, v in inputs.items()}
    with torch.no_grad():
        out = model.get_image_features(**inputs)  # (N, D)
    if normalize:
        out = out / out.norm(dim=-1, keepdim=True)
    return out


def encode_image_paths(
    model,
    processor,
    paths: List[Union[str, Path]],
    device: str,
    normalize: bool = True,
) -> torch.Tensor:
    """Encode images from file paths. Returns (N, D)."""
    images = [Image.open(p).convert("RGB") for p in paths]
    return encode_images(model, processor, images, device, normalize)


def encode_text(
    model,
    processor,
    texts: List[str],
    device: str,
    normalize: bool = True,
) -> torch.Tensor:
    """Encode text to CLIP text features. Returns (N, D)."""
    inputs = processor(text=texts, return_tensors="pt", padding=True, truncation=True)
    inputs = {k: v.to(device) for k, v in inputs.items()}
    with torch.no_grad():
        out = model.get_text_features(**inputs)  # (N, D)
    if normalize:
        out = out / out.norm(dim=-1, keepdim=True)
    return out


def video_to_frames(
    video_path: Union[str, Path],
    fps: float = 1.0,
    max_frames: int = 32,
    frame_size: int = 224,
) -> List[Image.Image]:
    """Sample frames from video file. Returns list of PIL Images (RGB, resized)."""
    if not HAS_CV2:
        raise ImportError("opencv-python is required for video loading. pip install opencv-python")
    path = str(video_path)
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise IOError(f"Cannot open video: {path}")
    video_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    step = max(1, int(video_fps / fps))  # sample every `step` frames
    frames = []
    for i in range(0, total_frames, step):
        if len(frames) >= max_frames:
            break
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ret, bgr = cap.read()
        if not ret:
            break
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        if frame_size:
            img = img.resize((frame_size, frame_size), Image.Resampling.BILINEAR)
        frames.append(img)
    cap.release()
    return frames


def encode_video_frames(
    model,
    processor,
    video_path: Union[str, Path],
    device: str,
    fps: float = 1.0,
    max_frames: int = 32,
    frame_size: int = 224,
    normalize: bool = True,
) -> torch.Tensor:
    """
    Load video, sample frames, encode with CLIP. Returns (T, D) where T = num frames.
    """
    frames = video_to_frames(video_path, fps=fps, max_frames=max_frames, frame_size=frame_size)
    if not frames:
        raise ValueError(f"No frames extracted from {video_path}")
    return encode_images(model, processor, frames, device, normalize)
