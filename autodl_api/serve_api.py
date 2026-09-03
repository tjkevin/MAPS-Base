#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BAGEL SFT 模型 HTTP API 服务（单卡 RTX5090，零三方依赖：仅 Python 标准库）
=================================================================================
设计要点（详见 ../BAGEL模型API服务设计-20260902.md）：

1. 复用 eval_suite/worker.py 中已实测验证的加载器：
     sft_nf4  —— SFT 合并权重 NF4 预量化包（常驻 ~8.8 GiB / 峰值 ~9.2 GiB）
     sft_bf16 —— SFT 合并权重 BF16 满血（常驻 ~27.2 GiB / 峰值 ~27.6 GiB）
     base_nf4 / base_bf16 也可选（--models 列表里加上即可）
   FP32 不提供：全 fp32 需 54.4 GiB，单卡 31.4 GiB 物理不可行。

2. 同一时刻只驻留一个模型：NF4(~9GiB) 与 BF16(~27.5GiB) 无法在 31.4GiB 卡共存；
   请求指定另一形态时自动「卸载旧模型 → 加载新模型」（串行，含全局推理锁）。

3. 【对后台实验零干扰的关键】--wait-for-gpu（默认开启）：
   任何模型加载前先轮询等待：(a) 系统中无 worker.py / run_all2.sh 进程；
   (b) 空闲显存 ≥ 该形态所需阈值（NF4 12 GiB / BF16 30 GiB）。
   => 即使实验正在跑，也可以现在就 nohup 启动本服务：它不占显存、一直等到实验
      结束后才自动加载模型开始服务。绝不与实验抢 GPU。

4. 鉴权：Bearer API Key（环境变量 BAGEL_API_KEY；未设置则自动生成并写入
   logs/api_key.txt，权限 600，启动日志中也会打印一次）。

接口：
   GET  /health                  健康检查（状态/已加载模型/显存/忙闲）
   GET  /v1/models               可用形态列表
   POST /v1/chat/completions     OpenAI 兼容（messages 内 image_url 支持 data:base64）
   POST /v1/infer                JSON：{model, prompt, image_base64|image_data_uri, ...}
   POST /v1/infer/file           multipart/form-data：image 文件 + prompt 字段

启动（实验结束后手动启动，或现在预启动等待）：
   cd /root/autodl-tmp/migration/eval_suite
   OMP_NUM_THREADS=8 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
     nohup /root/autodl-tmp/SERVICE/BAGEL/.conda-env/bin/python serve_api.py \
       --host 0.0.0.0 --port 6006 --models sft_nf4,sft_bf16 \
       > logs/serve_api.log 2>&1 &
"""
from __future__ import annotations

import argparse
import base64
import gc
import io
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
ALL_MODELS = ["sft_nf4", "sft_bf16", "base_nf4", "base_bf16"]
# 加载前要求的最小空闲显存（GiB），留足激活/波动余量
NEED_FREE_GIB = {"sft_nf4": 12.0, "base_nf4": 12.0, "sft_bf16": 30.0, "base_bf16": 30.0}
MAX_BODY_BYTES = 40 * 1024 * 1024  # 请求体上限 40MB（base64 图片）

STATE: Dict[str, Any] = {
    "loaded": None,          # 当前驻留模型名
    "inferencer": None,
    "info": None,
    "status": "starting",    # starting / waiting_gpu / loading / ready / error
    "last_event": "进程启动",
    "busy": False,
    "n_requests": 0,
}
_LOCK_LOAD = threading.Lock()    # 模型加载/切换串行
_LOCK_INFER = threading.Lock()   # 推理串行（单 GPU）


def log(*a: Any) -> None:
    print(f"[{time.strftime('%F %T')}]", *a, flush=True)


# ---------------------------------------------------------------------------
# GPU / 实验探测（只读，不占显存）
# ---------------------------------------------------------------------------
def gpu_free_gib() -> float:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=30)
        mib = float(out.stdout.strip().splitlines()[0].strip())
        return mib / 1024.0
    except Exception as e:
        log(f"[gate] nvidia-smi 查询失败: {e}；按 0 空闲处理")
        return 0.0


def experiment_running() -> bool:
    """是否还有 eval_suite 的实验进程（worker.py / run_all2.sh / run_all.sh）。"""
    try:
        out = subprocess.run(["pgrep", "-af", "worker\\.py|run_all2?\\.sh"],
                             capture_output=True, text=True, timeout=30)
        for line in out.stdout.splitlines():
            if "serve_api" in line:
                continue
            if "worker.py" in line or "run_all" in line:
                return True
    except Exception:
        pass
    return False


def wait_for_gpu(model_name: str, poll_sec: int = 30) -> None:
    """闸门：实验在跑或显存不足时一直等（不占 GPU），达标后返回。"""
    need = NEED_FREE_GIB[model_name]
    STATE["status"] = "waiting_gpu"
    while True:
        free = gpu_free_gib()
        running = experiment_running()
        if not running and free >= need:
            log(f"[gate] GPU 就绪：实验进程无，空闲 {free:.1f} GiB ≥ 需要 {need:.1f} GiB")
            return
        log(f"[gate] 等待加载 {model_name}：实验运行中={running}，空闲显存 {free:.1f}/{need:.1f} GiB；"
            f"{poll_sec}s 后重试（不占用 GPU）")
        time.sleep(poll_sec)


# ---------------------------------------------------------------------------
# 模型加载 / 切换 / 推理（复用 worker.py，已验证路径）
# ---------------------------------------------------------------------------
def _unload_locked() -> None:
    if STATE["inferencer"] is None:
        return
    log(f"[model] 卸载 {STATE['loaded']} 以切换形态")
    STATE["inferencer"] = None
    STATE["info"] = None
    STATE["loaded"] = None
    gc.collect()
    try:
        import torch
        torch.cuda.empty_cache()
    except Exception:
        pass


def ensure_loaded(model_name: str) -> None:
    """确保指定模型已驻留；不同则切换（卸载→闸门等待→加载）。"""
    with _LOCK_LOAD:
        if STATE["loaded"] == model_name and STATE["inferencer"] is not None:
            return
        STATE["status"] = "waiting_gpu" if experiment_running() else "loading"
        STATE["last_event"] = f"准备加载 {model_name}"
        _unload_locked()
        wait_for_gpu(model_name)
        STATE["status"] = "loading"
        STATE["last_event"] = f"正在加载 {model_name}"
        t0 = time.perf_counter()
        import worker  # 延迟导入：worker 顶层仅依赖 common（stdlib），torch 在加载器内部导入
        inferencer, info = worker.LOADERS[model_name]()
        STATE["inferencer"] = inferencer
        STATE["info"] = info
        STATE["loaded"] = model_name
        STATE["status"] = "ready"
        STATE["last_event"] = f"{model_name} 加载完成，用时 {time.perf_counter()-t0:.0f}s"
        log(f"[model] {STATE['last_event']}；常驻 {info.get('vram_after_load_gib')} GiB")


def run_inference(model_name: str, img, prompt: str, do_sample: bool,
                  temperature: float, max_new_tokens: int) -> Dict[str, Any]:
    ensure_loaded(model_name)
    import worker
    with _LOCK_INFER:
        STATE["busy"] = True
        t0 = time.perf_counter()
        try:
            out = worker.call_inferencer(
                STATE["inferencer"], img, prompt,
                do_sample=do_sample, temperature=temperature,
                max_new_tokens=max_new_tokens,
                autocast_bf16=bool(STATE["info"].get("autocast_bf16")),
                tag="api")
        finally:
            STATE["busy"] = False
    return {
        "model": model_name,
        "response": (out.get("text") or "").strip(),
        "infer_sec": round(out.get("_infer_sec", time.perf_counter() - t0), 3),
    }


# ---------------------------------------------------------------------------
# 图片与请求解析
# ---------------------------------------------------------------------------
def decode_image(b64_or_datauri: str):
    from PIL import Image
    s = b64_or_datauri.strip()
    if s.startswith("data:"):
        s = s.split(",", 1)[1]
    raw = base64.b64decode(s)
    return Image.open(io.BytesIO(raw)).convert("RGB")


def parse_chat_messages(messages: list):
    """OpenAI 兼容：从 messages 抽取 prompt 文本与 data:image base64。"""
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
# HTTP 处理
# ---------------------------------------------------------------------------
API_KEY: Optional[str] = None
MODELS: list = ["sft_nf4", "sft_bf16"]


class Handler(BaseHTTPRequestHandler):
    server_version = "BAGEL-API/1.0"

    def log_message(self, fmt, *args):  # 静默默认访问日志，用自定义日志
        pass

    # ---- 基础工具 ----
    def _send_json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code: int, msg: str):
        self._send_json(code, {"error": {"message": msg, "type": "bagel_api_error"}})

    def _auth_ok(self) -> bool:
        if not API_KEY:
            return True
        tok = self.headers.get("Authorization", "")
        if tok.startswith("Bearer ") and tok[7:].strip() == API_KEY:
            return True
        self._err(401, "未授权：需要 Header  Authorization: Bearer <API_KEY>")
        return False

    def _read_body(self) -> Optional[bytes]:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            self._err(400, "请求体为空")
            return None
        if n > MAX_BODY_BYTES:
            self._err(413, f"请求体 {n} 字节超过上限 {MAX_BODY_BYTES}")
            return None
        return self.rfile.read(n)

    # ---- 路由 ----
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/health":
            free = gpu_free_gib()
            self._send_json(200, {
                "status": STATE["status"], "loaded_model": STATE["loaded"],
                "busy": STATE["busy"], "last_event": STATE["last_event"],
                "available_models": MODELS, "n_requests": STATE["n_requests"],
                "gpu_free_gib": round(free, 2),
                "vram_resident_gib": (STATE["info"] or {}).get("vram_after_load_gib"),
                "time": time.strftime("%F %T"),
            })
        elif path == "/v1/models":
            self._send_json(200, {"object": "list", "data": [
                {"id": m, "object": "model",
                 "precision": "nf4" if m.endswith("nf4") else "bf16",
                 "vram_peak_gib": 9.2 if m.endswith("nf4") else 27.6}
                for m in MODELS]})
        else:
            self._err(404, f"未知路径: {self.path}")

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if not self._auth_ok():
            return
        body = self._read_body()
        if body is None:
            return
        try:
            if path == "/v1/chat/completions":
                self._handle_chat(body)
            elif path == "/v1/infer":
                self._handle_infer(body)
            elif path == "/v1/infer/file":
                self._handle_infer_file(body)
            else:
                self._err(404, f"未知路径: {self.path}")
        except Exception as e:
            log(f"[error] {path}: {type(e).__name__}: {e}")
            self._err(500, f"推理失败: {type(e).__name__}: {e}")

    # ---- 业务处理 ----
    def _dispatch(self, model: Optional[str], img, prompt: str,
                  do_sample: bool, temperature: float, max_new_tokens: int):
        if not prompt:
            self._err(400, "缺少 prompt/文本问题")
            return
        model = model or MODELS[0]
        if model not in MODELS:
            self._err(400, f"模型 {model} 不可用；可选: {MODELS}")
            return
        STATE["n_requests"] += 1
        log(f"[req] #{STATE['n_requests']} model={model} sample={do_sample} "
            f"T={temperature} max_new={max_new_tokens} prompt={prompt[:60]!r}")
        r = run_inference(model, img, prompt, do_sample, temperature, max_new_tokens)
        return r

    def _handle_chat(self, body: bytes):
        data = json.loads(body.decode("utf-8"))
        prompt, img_b64 = parse_chat_messages(data.get("messages", []))
        if img_b64 is None:
            self._err(400, "messages 中未找到 image_url（data:image/...;base64,...）")
            return
        img = decode_image(img_b64)
        r = self._dispatch(
            data.get("model"), img, prompt,
            do_sample=bool(data.get("temperature", 0)) and data.get("do_sample", True),
            temperature=float(data.get("temperature", 0.3) or 0.3),
            max_new_tokens=int(data.get("max_tokens", data.get("max_new_tokens", 256))))
        if r is None:
            return
        self._send_json(200, {
            "id": f"bagel-{int(time.time())}-{STATE['n_requests']}",
            "object": "chat.completion", "created": int(time.time()),
            "model": r["model"],
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": r["response"]}}],
            "usage": {"infer_sec": r["infer_sec"]},
        })

    def _handle_infer(self, body: bytes):
        data = json.loads(body.decode("utf-8"))
        img_b64 = data.get("image_base64") or data.get("image_data_uri")
        if not img_b64:
            self._err(400, "缺少 image_base64 / image_data_uri 字段")
            return
        img = decode_image(img_b64)
        r = self._dispatch(
            data.get("model"), img, data.get("prompt", ""),
            do_sample=bool(data.get("do_sample", False)),
            temperature=float(data.get("temperature", 0.3)),
            max_new_tokens=int(data.get("max_new_tokens", 256)))
        if r is None:
            return
        self._send_json(200, r)

    def _handle_infer_file(self, body: bytes):
        import cgi
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            self._err(400, "Content-Type 必须是 multipart/form-data")
            return
        fs = cgi.FieldStorage(fp=io.BytesIO(body),
                              headers={"Content-Type": ctype},
                              environ={"REQUEST_METHOD": "POST"})
        if "image" not in fs:
            self._err(400, "缺少文件字段 image")
            return
        from PIL import Image
        img = Image.open(io.BytesIO(fs["image"].file.read())).convert("RGB")
        form = fs["prompt"].value if "prompt" in fs else ""
        model = fs["model"].value if "model" in fs else None
        do_sample = (fs["do_sample"].value or "false").lower() in ("1", "true", "yes")
        temperature = float(fs["temperature"].value if "temperature" in fs else 0.3)
        max_new = int(fs["max_new_tokens"].value if "max_new_tokens" in fs else 256)
        r = self._dispatch(model, img, form, do_sample, temperature, max_new)
        if r is None:
            return
        self._send_json(200, r)


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
    os.chmod(keyfile, 0o600)
    return key


def main() -> None:
    global API_KEY, MODELS
    ap = argparse.ArgumentParser(description="BAGEL SFT HTTP API 服务（NF4 / BF16）")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=6006, help="AutoDL 自定义服务用 6006")
    ap.add_argument("--models", default="sft_nf4,sft_bf16",
                    help="逗号分隔，可选 sft_nf4/sft_bf16/base_nf4/base_bf16")
    ap.add_argument("--preload", default="", help="启动后预加载的模型（默认列表第一个）")
    ap.add_argument("--wait-for-gpu", dest="wait_gpu", action="store_true", default=True,
                    help="加载前等待实验结束且显存空闲（默认开，保证不干扰 nohup 实验）")
    ap.add_argument("--no-wait", dest="wait_gpu", action="store_false",
                    help="关闭闸门（仅在确认 GPU 空闲时使用）")
    args = ap.parse_args()

    MODELS = [m.strip() for m in args.models.split(",") if m.strip() in ALL_MODELS]
    if not MODELS:
        raise SystemExit(f"--models 无有效项；可选 {ALL_MODELS}")
    API_KEY = resolve_api_key()
    preload = args.preload.strip() or MODELS[0]

    if not args.wait_gpu:
        # 关闭闸门时直接替换 wait_for_gpu 为空操作
        globals()["wait_for_gpu"] = lambda *a, **k: None

    log("=" * 70)
    log(f"BAGEL API 启动：host={args.host} port={args.port} models={MODELS} preload={preload}")
    log(f"GPU 闸门(wait-for-gpu)={args.wait_gpu}；当前空闲显存 {gpu_free_gib():.1f} GiB；"
        f"实验进程运行中={experiment_running()}")
    log(f"API Key: {API_KEY}  （也存于 logs/api_key.txt，请求头 Authorization: Bearer <key>）")
    log("=" * 70)

    # 后台预加载（闸门等待期间 HTTP 已可响应 /health）
    def _preload():
        try:
            ensure_loaded(preload)
        except Exception as e:
            STATE["status"] = "error"
            STATE["last_event"] = f"预加载失败: {e}"
            log(f"[fatal] 预加载失败: {type(e).__name__}: {e}")
    threading.Thread(target=_preload, daemon=True).start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    log(f"HTTP 服务已监听 {args.host}:{args.port}（等待/加载期间 /health 可查）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("收到中断，退出")


if __name__ == "__main__":
    main()
