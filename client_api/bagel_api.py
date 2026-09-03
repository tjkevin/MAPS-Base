#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BAGEL-7B-MoT 局域网 HTTP API 服务（Windows + RTX 5070Ti + NF4 量化）
=================================================================================
用途：在本机（Windows 家庭/办公机）常驻 BAGEL 模型，供局域网内其他机器
      （或经 frp / SSH 反向隧道 / Tailscale 打通后的腾讯云系统）以 HTTP 调用：

        POST /v1/understand        图片理解（VQA，主要任务）
        POST /v1/generate          文生图
        POST /v1/edit              图片编辑/修正
        POST /v1/chat/completions  OpenAI 轻兼容接口（图片理解）
        POST /v1/understand/file   multipart 文件上传（curl/表单友好）
        POST /v1/transcribe        Whisper 音/视频转写（返回带时间轴的分段草稿，反馈#10）
        POST /v1/video-keyframes   视频关键帧抽取 + BAGEL 逐帧描述（视频主要内容简述，反馈#10）
        GET  /health               健康/状态检查（含 capabilities 能力列表）
        GET  /v1/models            模型信息

转写/抽帧为反馈#10 新增能力，依赖 ffmpeg 与 faster-whisper（可选依赖，懒加载）：
    - ffmpeg：Whisper 音频解码与视频抽帧均需要（Windows 下载 ffmpeg 并加入 PATH）；
    - faster-whisper：pip install faster-whisper（GPU 上自动用 CUDA，模型懒加载，
      首次 /v1/transcribe 时后台加载，期间返回 503 Retry-After）；
    - 模型规格可用环境变量 WHISPER_MODEL_SIZE（默认 medium）/ WHISPER_COMPUTE_TYPE
      （GPU 默认 float16）调整；媒体上传上限由 BAGEL_MEDIA_MAX_BODY_MB（默认 300MB）控制。

设计要点：
1. 模型加载链路与已验证可用的 `python app.py --mode 2` 完全一致（NF4 量化，
   16GB 显存可跑）；不引入第二套推理实现。
2. HTTP 层仅用 Python 标准库（http.server），无需 pip 安装任何额外包。
3. 单卡串行推理（全局锁），并发请求自动排队；模型在后台线程预加载，
   加载期间 /health 可查、推理接口返回 503（可轮询重试）。
4. 鉴权：Bearer API Key（环境变量 BAGEL_API_KEY；未设置则自动生成并写入
   logs/api_key.txt）。
5. 与 AutoDL 版 serve_api.py 的区别：那个脚本面向 Linux + eval_suite/worker.py
   （pgrep 闸门、多形态切换），本仓库没有 worker.py，故在本机不可用；
   本脚本直接复用仓库自带 app.py / inferencer.py 的加载与推理路径。

启动（在仓库根目录 d:\\BAGEL）：
    conda activate bagel
    python bagel_api.py --host 0.0.0.0 --port 6006
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

import numpy as np
import torch
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# ---------------------------------------------------------------------------
# 常量 / 全局状态
# ---------------------------------------------------------------------------
MODEL_ID = "bagel-7b-mot-nf4"
MAX_BODY_BYTES = 40 * 1024 * 1024  # 请求体上限 40MB（base64 图片）
# 反馈#10：音/视频媒体上传（转写/抽帧）请求体上限，默认 300MB，可用环境变量调整
MEDIA_MAX_BODY_BYTES = int(os.environ.get("BAGEL_MEDIA_MAX_BODY_MB", "300") or 300) * 1024 * 1024
OUTPUT_DIR = os.path.join(HERE, "outputs", "api")

# 反馈#10：Whisper 转写默认关键帧提示词（可被请求字段 prompt 覆盖）
DEFAULT_KEYFRAME_PROMPT = (
    "你是 MAPS 多模态数据采集处理平台的视频理解助手。这是从一段视频中按时间顺序抽取的关键帧。"
    "请用中文客观描述该画面：场景环境、出现的人物及其动作/表情/衣着、物体、画面中的文字信息，"
    "以及能推断的时间地点线索；只描述本帧可见内容，不要推测前后情节，不要杜撰。用 2-4 句话描述。"
)

# 文生图预设比例（长边固定 1024，与 app.py 一致）
IMAGE_RATIOS = {
    "1:1": (1024, 1024),
    "4:3": (768, 1024),
    "3:4": (1024, 768),
    "16:9": (576, 1024),
    "9:16": (1024, 576),
}

STATE: Dict[str, Any] = {
    "status": "starting",      # starting / loading / ready / error
    "last_event": "进程启动",
    "busy": False,
    "n_requests": 0,
    "inferencer": None,
    "load_sec": None,
}
_LOCK_INFER = threading.Lock()   # 单 GPU 推理串行（BAGEL 与 Whisper 共用，防并发显存竞争）
API_KEY: Optional[str] = None

# 反馈#10：Whisper 转写模型状态（懒加载：首次 /v1/transcribe 时后台加载）
# status: not_checked / loading / ready / error
WHISPER_STATE: Dict[str, Any] = {
    "status": "not_checked",
    "model": None,
    "size": "",
    "error": "",
    "load_sec": None,
}
_WHISPER_LOCK = threading.Lock()


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def whisper_deps_ok() -> Tuple[bool, str]:
    """检查 Whisper 转写依赖：ffmpeg（音频解码）+ faster-whisper（模型）。"""
    if shutil.which("ffmpeg") is None:
        return False, "未找到 ffmpeg（Whisper 音频解码需要，请安装 ffmpeg 并加入 PATH）"
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False, "未安装 faster-whisper（请在 GPU 环境执行 pip install faster-whisper）"
    return True, ""


def capabilities() -> list:
    """本机 API 实际具备的能力（/health 上报，云端据此路由）。"""
    caps = ["understand_image", "text_to_image", "image_editing"]
    ok, _ = whisper_deps_ok()
    if ok:
        caps.append("transcribe")        # 音/视频 Whisper 转写
    if shutil.which("ffmpeg") is not None:
        caps.append("video_keyframes")   # 视频抽帧 + BAGEL 描述
    return caps


def get_whisper_model():
    """
    获取 Whisper 模型（懒加载）。
    - 依赖缺失/加载失败：返回 None，WHISPER_STATE['status']='error'（调用方返回明确错误）；
    - 首次调用：后台线程加载，返回 None 且 status='loading'（调用方返回 503 重试）；
    - 就绪：返回模型对象。
    """
    if WHISPER_STATE["status"] == "ready":
        return WHISPER_STATE["model"]
    with _WHISPER_LOCK:
        if WHISPER_STATE["status"] == "ready":
            return WHISPER_STATE["model"]
        if WHISPER_STATE["status"] == "loading":
            return None
        ok, msg = whisper_deps_ok()
        if not ok:
            WHISPER_STATE["status"] = "error"
            WHISPER_STATE["error"] = msg
            return None

        def _load() -> None:
            t0 = time.perf_counter()
            try:
                from faster_whisper import WhisperModel
                size = (os.environ.get("WHISPER_MODEL_SIZE", "medium") or "medium").strip()
                device = "cuda" if torch.cuda.is_available() else "cpu"
                default_compute = "float16" if device == "cuda" else "int8"
                compute_type = (os.environ.get("WHISPER_COMPUTE_TYPE", default_compute) or default_compute).strip()
                log(f"[whisper] 加载 faster-whisper 模型 {size}（{device}/{compute_type}）…")
                model = WhisperModel(size, device=device, compute_type=compute_type)
                WHISPER_STATE.update(status="ready", model=model, size=size,
                                     load_sec=round(time.perf_counter() - t0, 1), error="")
                log(f"[whisper] 模型 {size} 加载完成，用时 {WHISPER_STATE['load_sec']}s")
            except Exception as e:
                WHISPER_STATE.update(status="error", error=f"{type(e).__name__}: {e}")
                log("[fatal] Whisper 模型加载失败：")
                log(traceback.format_exc())

        WHISPER_STATE["status"] = "loading"
        WHISPER_STATE["error"] = ""
        threading.Thread(target=_load, daemon=True).start()
        return None


def log(*a: Any) -> None:
    print(f"[{time.strftime('%F %T')}]", *a, flush=True)


# ---------------------------------------------------------------------------
# VAE 设备封装：官方 app.py 中 VAE 留在 CPU，文生图/编辑最终解码时
# latent 在 GPU、VAE 权重在 CPU 会设备不匹配；这里统一固定到 GPU(bf16)，
# 并对输入做设备/dtype 自适应（encode 的输入来自 CPU transform）。
# ---------------------------------------------------------------------------
class VAEWrap:
    def __init__(self, vae_model):
        self.vae = vae_model.to("cuda", dtype=torch.bfloat16).eval()
        for p in self.vae.parameters():
            p.requires_grad_(False)

    def encode(self, x):
        return self.vae.encode(x.to(device="cuda", dtype=torch.bfloat16))

    def decode(self, z):
        return self.vae.decode(z.to(device="cuda", dtype=torch.bfloat16))


# ---------------------------------------------------------------------------
# 模型加载（与 app.py --mode 2 NF4 完全相同的链路）
# ---------------------------------------------------------------------------
def build_inferencer(model_path: str):
    from accelerate import infer_auto_device_map, init_empty_weights
    from accelerate.utils import BnbQuantizationConfig, load_and_quantize_model

    from data.data_utils import add_special_tokens
    from data.transforms import ImageTransform
    from inferencer import InterleaveInferencer
    from modeling.autoencoder import load_ae
    from modeling.bagel import (
        Bagel, BagelConfig, Qwen2Config, Qwen2ForCausalLM,
        SiglipVisionConfig, SiglipVisionModel,
    )
    from modeling.qwen2 import Qwen2Tokenizer

    log(f"[model] 加载 BAGEL（NF4）：{model_path}")

    llm_config = Qwen2Config.from_json_file(os.path.join(model_path, "llm_config.json"))
    llm_config.qk_norm = True
    llm_config.tie_word_embeddings = False
    llm_config.layer_module = "Qwen2MoTDecoderLayer"

    vit_config = SiglipVisionConfig.from_json_file(os.path.join(model_path, "vit_config.json"))
    vit_config.rope = False
    vit_config.num_hidden_layers -= 1

    vae_model, vae_config = load_ae(local_path=os.path.join(model_path, "ae.safetensors"))
    vae_model = VAEWrap(vae_model)  # 固定到 GPU，避免生图解码设备不匹配

    config = BagelConfig(
        visual_gen=True,
        visual_und=True,
        llm_config=llm_config,
        vit_config=vit_config,
        vae_config=vae_config,
        vit_max_num_patch_per_side=70,
        connector_act='gelu_pytorch_tanh',
        latent_patch_size=2,
        max_latent_size=64,
    )

    with init_empty_weights():
        language_model = Qwen2ForCausalLM(llm_config)
        vit_model = SiglipVisionModel(vit_config)
        model = Bagel(language_model, vit_model, config)
        model.vit_model.vision_model.embeddings.convert_conv2d_to_linear(vit_config, meta=True)

    tokenizer = Qwen2Tokenizer.from_pretrained(model_path)
    tokenizer, new_token_ids, _ = add_special_tokens(tokenizer)

    vae_transform = ImageTransform(1024, 512, 16)
    vit_transform = ImageTransform(980, 224, 14)

    device_map = infer_auto_device_map(
        model,
        max_memory={i: "80GiB" for i in range(torch.cuda.device_count())},
        no_split_module_classes=["Bagel", "Qwen2MoTDecoderLayer"],
    )
    same_device_modules = [
        'language_model.model.embed_tokens',
        'time_embedder',
        'latent_pos_embed',
        'vae2llm',
        'llm2vae',
        'connector',
        'vit_pos_embed',
    ]
    if torch.cuda.device_count() == 1:
        first_device = device_map.get(same_device_modules[0], "cuda:0")
        for k in same_device_modules:
            if k in device_map:
                device_map[k] = first_device
            else:
                device_map[k] = "cuda:0"
    else:
        first_device = device_map.get(same_device_modules[0])
        for k in same_device_modules:
            if k in device_map:
                device_map[k] = first_device

    # NF4 量化（等价 app.py --mode 2）
    bnb_quantization_config = BnbQuantizationConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=False,
        bnb_4bit_quant_type="nf4",
    )
    model = load_and_quantize_model(
        model,
        weights_location=os.path.join(model_path, "ema.safetensors"),
        bnb_quantization_config=bnb_quantization_config,
        device_map=device_map,
        offload_folder=os.path.join(HERE, "offload"),
    ).eval()

    return InterleaveInferencer(
        model=model,
        vae_model=vae_model,
        tokenizer=tokenizer,
        vae_transform=vae_transform,
        vit_transform=vit_transform,
        new_token_ids=new_token_ids,
    )


def preload_model(model_path: str) -> None:
    STATE["status"] = "loading"
    STATE["last_event"] = "正在加载模型（NF4）"
    t0 = time.perf_counter()
    try:
        STATE["inferencer"] = build_inferencer(model_path)
        STATE["status"] = "ready"
        STATE["load_sec"] = round(time.perf_counter() - t0, 1)
        STATE["last_event"] = f"模型加载完成，用时 {STATE['load_sec']}s"
        log(f"[model] {STATE['last_event']}")
    except Exception as e:
        STATE["status"] = "error"
        STATE["last_event"] = f"模型加载失败: {type(e).__name__}: {e}"
        log("[fatal] 模型加载失败：")
        log(traceback.format_exc())


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def gpu_mem_gib() -> Tuple[float, float]:
    """返回 (空闲 GiB, 总 GiB)；torch 不可用时回退 nvidia-smi。"""
    try:
        free, total = torch.cuda.mem_get_info(0)
        return free / 1024**3, total / 1024**3
    except Exception:
        return -1.0, -1.0


def set_seed(seed: int) -> None:
    """与 app.py 一致：seed>0 才固定随机种子，0 为随机。"""
    if seed and seed > 0:
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed(seed)
            torch.cuda.manual_seed_all(seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False


def decode_image(b64_or_datauri: str) -> Image.Image:
    from data.data_utils import pil_img2rgb
    s = b64_or_datauri.strip()
    if s.startswith("data:"):
        s = s.split(",", 1)[1]
    raw = base64.b64decode(s)
    return pil_img2rgb(Image.open(io.BytesIO(raw)))


def image_to_jpeg_b64(img: Image.Image, quality: int = 95) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def save_output(img: Image.Image, tag: str) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    fn = os.path.join(OUTPUT_DIR, f"{time.strftime('%Y%m%d_%H%M%S')}_{tag}.jpg")
    img.convert("RGB").save(fn, format="JPEG", quality=95)
    return fn


def as_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def as_float(v: Any, default: float) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def as_int(v: Any, default: int) -> int:
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def resolve_image_shape(d: dict) -> Tuple[int, int]:
    """文生图尺寸：image_ratio 预设，或 image_size=[H,W]（吸附到 16 的倍数，≤1024）。"""
    size = d.get("image_size")
    if isinstance(size, (list, tuple)) and len(size) == 2:
        h, w = int(size[0]), int(size[1])
        h = max(16, min(1024, round(h / 16) * 16))
        w = max(16, min(1024, round(w / 16) * 16))
        return h, w
    ratio = str(d.get("image_ratio", "1:1"))
    if ratio not in IMAGE_RATIOS:
        raise ValueError(f"image_ratio 仅支持 {list(IMAGE_RATIOS)} 或用 image_size=[H,W]")
    return IMAGE_RATIOS[ratio]


# ---------------------------------------------------------------------------
# 三大任务
# ---------------------------------------------------------------------------
def task_understand(img: Image.Image, prompt: str, d: dict) -> Dict[str, Any]:
    inferencer = STATE["inferencer"]
    t0 = time.perf_counter()
    result = inferencer(
        image=img,
        text=prompt,
        think=as_bool(d.get("think"), False),
        understanding_output=True,
        do_sample=as_bool(d.get("do_sample"), False),
        text_temperature=as_float(d.get("temperature"), 0.3),
        max_think_token_n=as_int(d.get("max_new_tokens"), 512),
    )
    return {"response": (result.get("text") or "").strip(),
            "infer_sec": round(time.perf_counter() - t0, 3)}


def task_generate(prompt: str, d: dict) -> Dict[str, Any]:
    inferencer = STATE["inferencer"]
    set_seed(as_int(d.get("seed"), 0))
    image_shape = resolve_image_shape(d)
    think = as_bool(d.get("think"), False)
    t0 = time.perf_counter()
    result = inferencer(
        text=prompt,
        think=think,
        max_think_token_n=as_int(d.get("max_think_token_n"), 1024),
        do_sample=as_bool(d.get("do_sample"), False) if think else False,
        text_temperature=as_float(d.get("temperature"), 0.3),
        cfg_text_scale=as_float(d.get("cfg_text_scale"), 4.0),
        cfg_interval=[as_float(d.get("cfg_interval"), 0.4), 1.0],
        timestep_shift=as_float(d.get("timestep_shift"), 3.0),
        num_timesteps=as_int(d.get("num_timesteps"), 50),
        cfg_renorm_min=as_float(d.get("cfg_renorm_min"), 0.0),
        cfg_renorm_type=str(d.get("cfg_renorm_type", "global")),
        image_shapes=image_shape,
    )
    img = result.get("image")
    if img is None:
        raise RuntimeError("模型未返回图像（可检查 prompt 或稍后重试）")
    saved = save_output(img, "gen")
    return {
        "image_base64": image_to_jpeg_b64(img),
        "media_type": "image/jpeg",
        "image_shape": list(image_shape),
        "saved_path": saved,
        "thinking": (result.get("text") or "").strip() or None,
        "infer_sec": round(time.perf_counter() - t0, 3),
    }


def task_edit(img: Image.Image, prompt: str, d: dict) -> Dict[str, Any]:
    inferencer = STATE["inferencer"]
    set_seed(as_int(d.get("seed"), 0))
    think = as_bool(d.get("think"), False)
    t0 = time.perf_counter()
    result = inferencer(
        image=img,
        text=prompt,
        think=think,
        max_think_token_n=as_int(d.get("max_think_token_n"), 1024),
        do_sample=as_bool(d.get("do_sample"), False) if think else False,
        text_temperature=as_float(d.get("temperature"), 0.3),
        cfg_text_scale=as_float(d.get("cfg_text_scale"), 4.0),
        cfg_img_scale=as_float(d.get("cfg_img_scale"), 2.0),
        cfg_interval=[as_float(d.get("cfg_interval"), 0.0), 1.0],
        timestep_shift=as_float(d.get("timestep_shift"), 3.0),
        num_timesteps=as_int(d.get("num_timesteps"), 50),
        cfg_renorm_min=as_float(d.get("cfg_renorm_min"), 0.0),
        cfg_renorm_type=str(d.get("cfg_renorm_type", "text_channel")),
    )
    out = result.get("image")
    if out is None:
        raise RuntimeError("模型未返回图像（可检查 prompt/输入图或稍后重试）")
    saved = save_output(out, "edit")
    return {
        "image_base64": image_to_jpeg_b64(out),
        "media_type": "image/jpeg",
        "image_shape": list(out.size[::-1]),
        "saved_path": saved,
        "thinking": (result.get("text") or "").strip() or None,
        "infer_sec": round(time.perf_counter() - t0, 3),
    }


# ---------------------------------------------------------------------------
# 反馈#10：Whisper 音/视频转写（faster-whisper 懒加载）
# ---------------------------------------------------------------------------
def task_transcribe(media_path: str, fields: Dict[str, str]) -> Dict[str, Any]:
    """
    用 faster-whisper 转写音/视频，返回带时间轴的分段草稿。
    fields: language（空=自动检测）、model_size（预留）、beam_size、vad_filter。
    """
    model = WHISPER_STATE["model"]
    t0 = time.perf_counter()
    lang = (fields.get("language") or "").strip() or None
    beam_size = as_int(fields.get("beam_size"), 5)
    vad = as_bool(fields.get("vad_filter"), True)
    segments, info = model.transcribe(
        media_path,
        language=lang,
        beam_size=beam_size,
        vad_filter=vad,
    )
    segs = []
    texts = []
    for s in segments:
        text = (getattr(s, "text", "") or "").strip()
        if not text:
            continue
        segs.append({
            "start": round(float(getattr(s, "start", 0.0) or 0.0), 2),
            "end": round(float(getattr(s, "end", 0.0) or 0.0), 2),
            "text": text,
        })
        texts.append(text)
    return {
        "text": "\n".join(texts),
        "segments": segs,
        "language": str(getattr(info, "language", None) or lang or ""),
        "duration": round(float(getattr(info, "duration", 0.0) or 0.0), 2),
        "infer_sec": round(time.perf_counter() - t0, 3),
    }


def _ffprobe_duration(media_path: str) -> float:
    """用 ffprobe 取媒体时长（秒）；失败返回 0.0。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", media_path],
            capture_output=True, timeout=60,
        )
        data = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
        return float((data.get("format") or {}).get("duration") or 0.0)
    except Exception:
        return 0.0


def task_video_keyframes(video_path: str, fields: Dict[str, str]) -> Dict[str, Any]:
    """
    视频关键帧简述（反馈#10）：ffmpeg 均匀抽 N 帧 → BAGEL 逐帧描述 → 汇总为视频主要内容简述。
    fields: max_frames（默认 6，上限 12）、prompt（自定义帧描述提示词）。
    """
    t0 = time.perf_counter()
    duration = _ffprobe_duration(video_path)
    n = max(1, min(12, as_int(fields.get("max_frames"), 6)))
    prompt = (fields.get("prompt") or "").strip() or DEFAULT_KEYFRAME_PROMPT

    tmpdir = tempfile.mkdtemp(prefix="bagel_kf_")
    keyframes = []
    try:
        for i in range(n):
            ts = duration * (i + 0.5) / n if duration > 0 else 0.0
            frame_path = os.path.join(tmpdir, f"f_{i:02d}.jpg")
            # -ss 置于 -i 前为快速定位；短视频误差可忽略
            cmd = ["ffmpeg", "-y", "-ss", f"{ts:.2f}", "-i", video_path,
                   "-frames:v", "1", "-q:v", "3", frame_path]
            try:
                subprocess.run(cmd, capture_output=True, timeout=90)
            except Exception:
                continue
            if not os.path.isfile(frame_path):
                continue
            try:
                from data.data_utils import pil_img2rgb
                img = pil_img2rgb(Image.open(frame_path))
                r = task_understand(img, prompt, {"do_sample": False, "temperature": 0.3,
                                                  "max_new_tokens": 512})
                keyframes.append({"t": round(ts, 2), "description": (r.get("response") or "").strip()})
            except Exception as e:
                log(f"[keyframes] 第 {i+1} 帧描述失败: {type(e).__name__}: {e}")
                keyframes.append({"t": round(ts, 2), "description": f"（该帧描述失败：{type(e).__name__}）"})
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if not keyframes:
        raise RuntimeError("关键帧抽取失败：请确认 ffmpeg 可用且视频文件有效")

    def _hms(sec: float) -> str:
        sec = int(sec)
        return f"{sec // 60:02d}:{sec % 60:02d}"

    lines = [f"[{_hms(k['t'])}] {k['description']}" for k in keyframes]
    dur_txt = f"约 {duration:.0f} 秒" if duration > 0 else "时长未知"
    summary = (f"视频时长{dur_txt}，按时间顺序均匀抽取 {len(keyframes)} 个关键帧，"
               f"由 BAGEL 逐帧描述如下：\n" + "\n".join(lines))
    return {
        "keyframes": keyframes,
        "summary": summary,
        "duration": round(duration, 2),
        "frame_count": len(keyframes),
        "infer_sec": round(time.perf_counter() - t0, 3),
    }


# ---------------------------------------------------------------------------
# OpenAI 兼容消息解析
# ---------------------------------------------------------------------------
def parse_chat_messages(messages: list):
    texts, img_b64 = [], None
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    texts.append(part.get("text", ""))
                elif part.get("type") == "image_url":
                    url = (part.get("image_url") or {}).get("url", "")
                    if url.startswith("data:image"):
                        img_b64 = url
    return "\n".join(t for t in texts if t), img_b64


# ---------------------------------------------------------------------------
# 极简 multipart/form-data 解析（标准库，不依赖已弃用的 cgi 模块）
# ---------------------------------------------------------------------------
def parse_multipart(body: bytes, content_type: str) -> Tuple[Dict[str, str], Dict[str, Tuple[str, bytes]]]:
    fields: Dict[str, str] = {}
    files: Dict[str, Tuple[str, bytes]] = {}
    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type)
    if not m:
        return fields, files
    boundary = (m.group(1) or m.group(2)).strip().encode("ascii")
    for part in body.split(b"--" + boundary):
        if not part or part in (b"--\r\n", b"--", b"\r\n"):
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        header_blob, _, blob = part.partition(b"\r\n\r\n")
        if not _:
            continue
        headers = header_blob.decode("utf-8", "replace")
        cd = re.search(r'Content-Disposition:\s*form-data;\s*name="([^"]+)"(?:;\s*filename="([^"]*)")?',
                       headers, re.IGNORECASE)
        if not cd:
            continue
        name, filename = cd.group(1), cd.group(2)
        if filename is not None:
            files[name] = (filename, blob)
        else:
            fields[name] = blob.decode("utf-8", "replace").strip()
    return fields, files


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "BAGEL-LAN-API/1.0"

    def log_message(self, fmt, *args):
        pass  # 静默默认访问日志，使用自定义日志

    # ---- 基础 ----
    def _send_json(self, code: int, obj: dict, extra_headers: Optional[dict] = None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code: int, msg: str, extra_headers: Optional[dict] = None):
        self._send_json(code, {"error": {"message": msg, "type": "bagel_api_error"}}, extra_headers)

    def _auth_ok(self) -> bool:
        if not API_KEY:
            return True
        tok = self.headers.get("Authorization", "")
        if tok.startswith("Bearer ") and tok[7:].strip() == API_KEY:
            return True
        self._err(401, "未授权：需要 Header  Authorization: Bearer <API_KEY>")
        return False

    def _read_body(self, limit: Optional[int] = None) -> Optional[bytes]:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            self._err(400, "请求体为空")
            return None
        max_bytes = int(limit or MAX_BODY_BYTES)
        if n > max_bytes:
            self._err(413, f"请求体 {n} 字节超过上限 {max_bytes}（{max_bytes // 1024 // 1024}MB）")
            return None
        return self.rfile.read(n)

    def _require_ready(self) -> bool:
        if STATE["status"] == "ready" and STATE["inferencer"] is not None:
            return True
        self._err(503, f"模型尚未就绪（status={STATE['status']}：{STATE['last_event']}），请稍后轮询 /health",
                  {"Retry-After": "10"})
        return False

    # ---- 路由 ----
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/":
            self._send_json(200, {"service": "BAGEL-7B-MoT LAN API (NF4)",
                                  "endpoints": ["/health", "/v1/models", "/v1/understand",
                                                "/v1/generate", "/v1/edit",
                                                "/v1/chat/completions", "/v1/understand/file",
                                                "/v1/transcribe", "/v1/video-keyframes"]})
        elif path == "/health":
            free, total = gpu_mem_gib()
            self._send_json(200, {
                "status": STATE["status"],
                "model": MODEL_ID if STATE["status"] == "ready" else None,
                "busy": STATE["busy"],
                "last_event": STATE["last_event"],
                "n_requests": STATE["n_requests"],
                "gpu_free_gib": round(free, 2),
                "gpu_total_gib": round(total, 2),
                "load_sec": STATE["load_sec"],
                # 反馈#10：能力列表（云端按此路由转写/抽帧任务）与 Whisper 模型状态
                "capabilities": capabilities(),
                "whisper_status": WHISPER_STATE["status"],
                "whisper_model": (f"whisper-{WHISPER_STATE['size']}" if WHISPER_STATE["status"] == "ready" else None),
                "whisper_error": WHISPER_STATE["error"] or None,
                "ffmpeg": ffmpeg_available(),
                "time": time.strftime("%F %T"),
            })
        elif path == "/v1/models":
            if not self._auth_ok():
                return
            self._send_json(200, {"object": "list", "data": [
                {"id": MODEL_ID, "object": "model", "precision": "nf4",
                 "capabilities": ["image_understanding", "text_to_image", "image_editing"]}]})
        else:
            self._err(404, f"未知路径: {self.path}")

    # 反馈#10：媒体上传类端点（音/视频文件）使用更大的请求体上限
    _MEDIA_ENDPOINTS = ("/v1/transcribe", "/v1/video-keyframes")

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if not self._auth_ok():
            return
        body_limit = MEDIA_MAX_BODY_BYTES if path in self._MEDIA_ENDPOINTS else MAX_BODY_BYTES
        body = self._read_body(limit=body_limit)
        if body is None:
            return
        try:
            if path == "/v1/understand":
                self._handle_understand(body)
            elif path == "/v1/generate":
                self._handle_generate(body)
            elif path == "/v1/edit":
                self._handle_edit(body)
            elif path == "/v1/chat/completions":
                self._handle_chat(body)
            elif path == "/v1/understand/file":
                self._handle_understand_file(body)
            elif path == "/v1/transcribe":
                self._handle_transcribe(body)
            elif path == "/v1/video-keyframes":
                self._handle_video_keyframes(body)
            else:
                self._err(404, f"未知路径: {self.path}")
        except Exception as e:
            log(f"[error] {path}: {type(e).__name__}: {e}")
            log(traceback.format_exc())
            self._err(500, f"推理失败: {type(e).__name__}: {e}")

    # ---- 推理统一入口（全局串行锁） ----
    def _run_task(self, tag: str, model_name: str, fn):
        if not self._require_ready():
            return
        if not model_name:
            self._err(400, "缺少 prompt/文本指令")
            return
        STATE["n_requests"] += 1
        req_id = STATE["n_requests"]
        log(f"[req] #{req_id} task={tag} prompt={str(model_name)[:80]!r}")
        with _LOCK_INFER:
            STATE["busy"] = True
            t0 = time.perf_counter()
            try:
                r = fn()
            finally:
                STATE["busy"] = False
        r["model"] = MODEL_ID
        log(f"[req] #{req_id} 完成，用时 {time.perf_counter() - t0:.1f}s")
        self._send_json(200, r)

    # ---- 各接口 ----
    def _handle_understand(self, body: bytes):
        d = json.loads(body.decode("utf-8"))
        img_b64 = d.get("image_base64") or d.get("image_data_uri")
        if not img_b64:
            self._err(400, "缺少 image_base64 / image_data_uri 字段")
            return
        prompt = str(d.get("prompt", "")).strip()
        if not prompt:
            self._err(400, "缺少 prompt 字段（图片理解问题）")
            return
        img = decode_image(img_b64)
        self._run_task("understand", prompt, lambda: task_understand(img, prompt, d))

    def _handle_generate(self, body: bytes):
        d = json.loads(body.decode("utf-8"))
        prompt = str(d.get("prompt", "")).strip()
        if not prompt:
            self._err(400, "缺少 prompt 字段（文生图提示词）")
            return
        self._run_task("generate", prompt, lambda: task_generate(prompt, d))

    def _handle_edit(self, body: bytes):
        d = json.loads(body.decode("utf-8"))
        img_b64 = d.get("image_base64") or d.get("image_data_uri")
        if not img_b64:
            self._err(400, "缺少 image_base64 / image_data_uri 字段（待编辑图片）")
            return
        prompt = str(d.get("prompt", "")).strip()
        if not prompt:
            self._err(400, "缺少 prompt 字段（编辑指令）")
            return
        img = decode_image(img_b64)
        self._run_task("edit", prompt, lambda: task_edit(img, prompt, d))

    def _handle_chat(self, body: bytes):
        d = json.loads(body.decode("utf-8"))
        prompt, img_b64 = parse_chat_messages(d.get("messages", []))
        if not img_b64:
            self._err(400, "messages 中未找到 image_url（data:image/...;base64,...）")
            return
        if not prompt.strip():
            self._err(400, "messages 中未找到文本问题")
            return
        img = decode_image(img_b64)
        params = {
            "think": d.get("think", False),
            "do_sample": bool(d.get("temperature", 0)),
            "temperature": as_float(d.get("temperature"), 0.3),
            "max_new_tokens": as_int(d.get("max_tokens", d.get("max_new_tokens")), 512),
        }

        def _do():
            r = task_understand(img, prompt, params)
            return {
                "id": f"bagel-{int(time.time())}-{STATE['n_requests']}",
                "object": "chat.completion", "created": int(time.time()),
                "model": MODEL_ID,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": r["response"]}}],
                "usage": {"infer_sec": r["infer_sec"]},
            }

        self._run_task("chat(understand)", prompt, _do)

    def _handle_understand_file(self, body: bytes):
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            self._err(400, "Content-Type 必须是 multipart/form-data")
            return
        fields, files = parse_multipart(body, ctype)
        if "image" not in files:
            self._err(400, "缺少文件字段 image")
            return
        from data.data_utils import pil_img2rgb
        prompt = fields.get("prompt", "").strip()
        if not prompt:
            self._err(400, "缺少表单字段 prompt")
            return
        img = pil_img2rgb(Image.open(io.BytesIO(files["image"][1])))
        d = {
            "think": fields.get("think", "false"),
            "do_sample": fields.get("do_sample", "false"),
            "temperature": fields.get("temperature", 0.3),
            "max_new_tokens": fields.get("max_new_tokens", 512),
        }
        self._run_task("understand/file", prompt, lambda: task_understand(img, prompt, d))

    # ---- 反馈#10：Whisper 音/视频转写（返回带时间轴的分段草稿） ----
    def _handle_transcribe(self, body: bytes):
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            self._err(400, "Content-Type 必须是 multipart/form-data（file 字段上传音频/视频）")
            return
        ok, msg = whisper_deps_ok()
        if not ok:
            # 501：依赖缺失为不可重试错误（云端按 FatalBackendError 处理，直接提示运维）
            self._err(501, f"转写依赖未就绪：{msg}。请在 GPU 机安装 ffmpeg 并执行 pip install faster-whisper 后重启本服务")
            return
        fields, files = parse_multipart(body, ctype)
        if "file" not in files:
            self._err(400, "缺少文件字段 file（音频或视频）")
            return
        fname, blob = files["file"]
        suffix = os.path.splitext(fname or "")[1] or ".bin"
        fd, tmp_path = tempfile.mkstemp(prefix="bagel_asr_", suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)

        model = get_whisper_model()
        if model is None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            if WHISPER_STATE["status"] == "error":
                self._err(500, f"Whisper 模型加载失败：{WHISPER_STATE['error']}")
            else:
                self._err(503, "Whisper 模型正在加载中（首次转写需加载模型，请稍候重试）",
                          {"Retry-After": "15"})
            return

        STATE["n_requests"] += 1
        req_id = STATE["n_requests"]
        log(f"[req] #{req_id} task=transcribe file={fname!r} size={len(blob) // 1024}KB")
        with _LOCK_INFER:
            STATE["busy"] = True
            t0 = time.perf_counter()
            try:
                r = task_transcribe(tmp_path, fields)
            finally:
                STATE["busy"] = False
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        r["model"] = f"whisper-{WHISPER_STATE['size']}"
        r["backend"] = "local"
        r["protocol"] = "lan"
        log(f"[req] #{req_id} 转写完成，用时 {time.perf_counter() - t0:.1f}s，"
            f"分段 {len(r.get('segments') or [])} 条")
        self._send_json(200, r)

    # ---- 反馈#10：视频关键帧抽取 + BAGEL 逐帧描述（视频主要内容简述） ----
    def _handle_video_keyframes(self, body: bytes):
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            self._err(400, "Content-Type 必须是 multipart/form-data（file 字段上传视频）")
            return
        if shutil.which("ffmpeg") is None:
            self._err(501, "抽帧依赖未就绪：未找到 ffmpeg，请在 GPU 机安装 ffmpeg 并加入 PATH 后重启本服务")
            return
        fields, files = parse_multipart(body, ctype)
        if "file" not in files:
            self._err(400, "缺少文件字段 file（视频）")
            return
        fname, blob = files["file"]
        suffix = os.path.splitext(fname or "")[1] or ".bin"
        fd, tmp_path = tempfile.mkstemp(prefix="bagel_kfin_", suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)

        def _do():
            try:
                r = task_video_keyframes(tmp_path, fields)
            finally:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            r["backend"] = "local"
            r["protocol"] = "lan"
            return r

        # 逐帧描述需要 BAGEL 模型就绪（_run_task 内含 _require_ready 与全局串行锁）
        self._run_task("video-keyframes", fname or "video", _do)


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def resolve_api_key() -> str:
    key = os.environ.get("BAGEL_API_KEY", "").strip()
    keyfile = os.path.join(HERE, "logs", "api_key.txt")
    if key:
        return key
    if os.path.isfile(keyfile):
        key = open(keyfile, encoding="utf-8").read().strip()
        if key:
            return key
    key = secrets.token_urlsafe(16)
    os.makedirs(os.path.dirname(keyfile), exist_ok=True)
    with open(keyfile, "w", encoding="utf-8") as f:
        f.write(key)
    try:
        os.chmod(keyfile, 0o600)
    except OSError:
        pass  # Windows 上 chmod 语义有限，忽略
    return key


def main() -> None:
    global API_KEY
    ap = argparse.ArgumentParser(description="BAGEL-7B-MoT 局域网 HTTP API（NF4，Windows/RTX5070Ti）")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址，局域网访问用 0.0.0.0")
    ap.add_argument("--port", type=int, default=6006)
    ap.add_argument("--model-path", default=os.path.join(HERE, "models", "BAGEL-7B-MoT"),
                    help="模型权重目录（含 ema.safetensors / ae.safetensors）")
    args = ap.parse_args()

    # Windows 控制台/重定向日志的 UTF-8 兜底
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not os.path.isfile(os.path.join(args.model_path, "ema.safetensors")):
        raise SystemExit(f"模型权重不存在：{args.model_path}（应包含 ema.safetensors 等文件）")

    API_KEY = resolve_api_key()

    log("=" * 72)
    log(f"BAGEL 局域网 API 启动：host={args.host} port={args.port} model=NF4")
    log(f"模型目录：{args.model_path}")
    free, total = gpu_mem_gib()
    if total > 0:
        log(f"GPU 显存：空闲 {free:.1f} / 总 {total:.1f} GiB")
    log(f"API Key: {API_KEY}")
    log("（也存于 logs/api_key.txt；请求头需带 Authorization: Bearer <key>）")
    log("=" * 72)

    threading.Thread(target=preload_model, args=(args.model_path,), daemon=True).start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    log(f"HTTP 服务已监听 {args.host}:{args.port}（模型后台加载中，/health 可查状态）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("收到中断，退出")


if __name__ == "__main__":
    main()
