"""
模型服务多后端路由层
====================
云端不运行任何模型，统一通过本模块调用外部推理服务：

- autodl   : BAGEL 满血版（autoDL 租用卡，SFT VQA 图文问答），serve_api.py：
             GET /health + POST /v1/infer（autodl_v1 协议，JSON+base64，Bearer 鉴权）
- local    : BAGEL 优化版（局域网本地 GPU 机，NF4，轻量快速），bagel_api.py：
             POST /v1/understand + Whisper 转写/抽帧（lan 协议）
- deepseek : DeepSeek 云端大模型 API（OpenAI 兼容，无需 GPU），受管理员配额约束
- auto     : 按任务内容自动路由（纯文本类 -> deepseek；图片/媒体理解 -> 默认 GPU 端点）

说明：云端仓库只负责任务编排、队列调度与结果管理；所有涉及 GPU / 大量算力的
图片理解工作都通过上述三类外部 API 完成。autodl 与 local 均为 BAGEL 系列模型，
但通信协议不同（autodl_v1 / lan），由 gpu_protocol() 按 BAGEL_<TARGET>_PROTOCOL 选择。

Worker（scripts/bagel_worker.py）与 Web 预检共用本模块，保证路由与配额口径一致。
"""
import base64
import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

import redis
import requests


# 支持的后端
BACKEND_AUTODL = "autodl"      # BAGEL 满血版（autoDL 租用卡，全功能图片理解）
BACKEND_LOCAL = "local"        # BAGEL 优化版（局域网本地 GPU 机，轻量快速）
BACKEND_DEEPSEEK = "deepseek"  # DeepSeek 云端大模型 API（纯文本，无需 GPU）
BACKEND_AUTO = "auto"          # 按任务内容自动路由
BACKEND_BAGEL = "bagel"        # 兼容旧值：等同于默认 GPU 端点（MODEL_DEFAULT_GPU_BACKEND）

GPU_BACKENDS = (BACKEND_AUTODL, BACKEND_LOCAL)

# GPU 后端通信协议
PROTOCOL_INFER = "infer"       # 旧版 BAGEL 协议：POST /infer（multipart，全模态）
PROTOCOL_LAN = "lan"           # 局域网 NF4 优化版 API：GET /health + POST /v1/*（图片理解/转写/抽帧）
PROTOCOL_AUTODL_V1 = "autodl_v1"  # autoDL serve_api.py（2026-09 版）：GET /health + POST /v1/infer（JSON+base64，Bearer）
# 仅支持单图理解的协议（音/视频任务不能落到这些端点）：
# - lan：局域网 NF4 优化版，图片理解 + Whisper 转写/抽帧（转写走独立端点，理解仅图片）
# - autodl_v1：autoDL SFT VQA 服务（sft_nf4/sft_bf16），单图问答
IMAGE_ONLY_PROTOCOLS = (PROTOCOL_LAN, PROTOCOL_AUTODL_V1)

# 图片-only 协议（lan 局域网 NF4 优化版、autodl_v1 autoDL SFT VQA 服务）均不支持音/视频；
# 音视频任务走 lan 协议的转写/抽帧端点（Whisper + ffmpeg，能力以 /health 的 capabilities 为准）。
# 反馈#10：lan 协议新增转写/抽帧端点（Whisper + ffmpeg，能力以 /health 的 capabilities 为准）。
LAN_SUPPORTED_MODALITIES = ("image",)

# 反馈#10：任务类型（payload.options.task_kind）
TASK_KIND_UNDERSTAND = "understand"  # 默认：图片/媒体理解（BAGEL / DeepSeek 文本）
TASK_KIND_TRANSCRIBE = "transcribe"  # Whisper 音/视频转写（可附带关键帧简述、外部大模型详述）

# lan 端点能力名（与 bagel_api.py /health 的 capabilities 对齐）
CAP_TRANSCRIBE = "transcribe"
CAP_VIDEO_KEYFRAMES = "video_keyframes"

# 图片理解默认提示词（未显式传 options.prompt 时使用）：要求 BAGEL 输出详细中文画面描述
DEFAULT_BAGEL_IMAGE_PROMPT = (
    "你是 MAPS 多模态数据采集处理平台的图像理解助手。请仔细观察这张图片，用中文完整、"
    "客观地描述图片内容：画面主体、场景环境、人物的动作与表情、图片中出现的文字信息、"
    "以及时间、地点等线索；按条理输出一段详细描述，不要遗漏关键信息，也不要杜撰图片中没有的内容。"
)

# 反馈#10：视频关键帧逐帧描述默认提示词（与 GPU 端 bagel_api.py 保持一致，可用 options 覆盖）
DEFAULT_KEYFRAME_PROMPT = (
    "你是 MAPS 多模态数据采集处理平台的视频理解助手。这是从一段视频中按时间顺序抽取的关键帧。"
    "请用中文客观描述该画面：场景环境、出现的人物及其动作/表情/衣着、物体、画面中的文字信息，"
    "以及能推断的时间地点线索；只描述本帧可见内容，不要推测前后情节，不要杜撰。用 2-4 句话描述。"
)

# 局域网 API 请求体上限 40MB，base64 膨胀约 4/3；原图超过该字节数时先压缩
LAN_MAX_IMAGE_BYTES = 28 * 1024 * 1024

# Redis 配额键前缀
_QUOTA_DAILY_PREFIX = "deepseek:quota:daily:"
_QUOTA_MONTHLY_PREFIX = "deepseek:quota:monthly:"


class BackendError(RuntimeError):
    """后端调用错误（含配额超限）。"""


class QuotaExceeded(BackendError):
    """DeepSeek 配额超限。"""


class FatalBackendError(BackendError):
    """不可重试的后端错误（配置缺失、HTTP 4xx、payload 非法等）：重试不会成功，直接入死信。"""


def _raise_for_status(resp: requests.Response) -> None:
    """
    raise_for_status 的包装：
    - HTTP 4xx（除 429 限流）为客户端错误，重试无意义 -> FatalBackendError（直接判失败入死信）；
    - 429 / 5xx / 网络错误仍抛原始 requests 异常，交由 worker 按退避重试。
    """
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        status = getattr(resp, "status_code", 0) or 0
        if 400 <= status < 500 and status != 429:
            body = (getattr(resp, "text", "") or "")[:200]
            raise FatalBackendError(f"HTTP {status} 客户端错误（重试无意义）: {body}") from e
        raise


# ---------------- 后端路由 ----------------

def select_backend(payload: Dict[str, Any], default_backend: str,
                   default_gpu: str = BACKEND_LOCAL) -> str:
    """
    决定任务使用的后端，返回 autodl / local / deepseek 之一。

    - 显式指定：autodl（满血版）/ local（优化版）/ deepseek 直接采用；
      bagel 为旧值，映射到默认 GPU 端点 default_gpu。
    - auto 模式：含文本提示且无媒体文件 -> deepseek；图片/媒体理解 -> 默认 GPU 端点。
    """
    default_gpu = default_gpu if default_gpu in GPU_BACKENDS else BACKEND_LOCAL
    backend = (payload.get("backend") or default_backend or BACKEND_AUTO).strip().lower()

    if backend == BACKEND_DEEPSEEK:
        return BACKEND_DEEPSEEK
    if backend in GPU_BACKENDS:
        return backend
    if backend == BACKEND_BAGEL:
        # 旧值兼容：bagel 等同于默认 GPU 端点
        return default_gpu
    if backend == BACKEND_AUTO:
        has_file = bool(payload.get("file_path") or payload.get("filename"))
        has_text = bool((payload.get("options") or {}).get("prompt") or payload.get("text"))
        # DeepSeek 为纯文本模型：图片/媒体文件理解必须走 GPU 端点（autodl/local）
        return BACKEND_DEEPSEEK if (has_text and not has_file) else default_gpu
    return default_gpu


def gpu_protocol(target: str, cfg) -> str:
    """
    返回 GPU 后端通信协议：
    - lan        ：局域网 NF4 优化版（client_api/bagel_api.py），图片理解 + Whisper 转写/抽帧；
    - autodl_v1  ：autoDL 2026-09 版 serve_api.py，GET /health + POST /v1/infer（JSON+base64，
                    SFT VQA 单图问答，模型形态 sft_nf4/sft_bf16 可切换）；
    - infer      ：旧版 BAGEL 协议（POST /infer，multipart 全模态，仅旧服务兼容用）。
    - target=local 默认 lan；target=autodl 默认 autodl_v1；
    - 可由 BAGEL_<TARGET>_PROTOCOL 环境变量覆盖。
    """
    attr = f"BAGEL_{target.upper()}_PROTOCOL"
    proto = str(getattr(cfg, attr, "") or "").strip().lower()
    if proto in (PROTOCOL_LAN, PROTOCOL_INFER, PROTOCOL_AUTODL_V1):
        return proto
    # 未配置时的内置默认
    if target == BACKEND_LOCAL:
        return PROTOCOL_LAN
    return PROTOCOL_AUTODL_V1


def gpu_endpoint(target: str, cfg, payload: Dict[str, Any]) -> Tuple[str, str]:
    """
    返回指定 GPU 后端的 (base_url, token)。
    - payload.bagel_service_url 显式覆盖时优先（向后兼容）；
    - autodl/local 分别取各自的 URL/TOKEN，未配置时回落到 BAGEL_SERVICE_URL/TOKEN。
    """
    override = (payload.get("bagel_service_url") or "").strip()
    if override:
        return override.rstrip("/"), (getattr(cfg, "BAGEL_SERVICE_TOKEN", "") or "")

    if target == BACKEND_AUTODL:
        url = getattr(cfg, "BAGEL_AUTODL_SERVICE_URL", "") or getattr(cfg, "BAGEL_SERVICE_URL", "")
        token = getattr(cfg, "BAGEL_AUTODL_SERVICE_TOKEN", "") or getattr(cfg, "BAGEL_SERVICE_TOKEN", "")
    else:  # local
        url = getattr(cfg, "BAGEL_LOCAL_SERVICE_URL", "") or getattr(cfg, "BAGEL_SERVICE_URL", "")
        token = getattr(cfg, "BAGEL_LOCAL_SERVICE_TOKEN", "") or getattr(cfg, "BAGEL_SERVICE_TOKEN", "")
    return (url or "").rstrip("/"), (token or "")


# ---------------- GPU 后端健康检查 / 故障切换 ----------------

_HEALTH_PREFIX = "gpu:health:"


def _lan_probe(base_url: str, timeout: float) -> Tuple[bool, str, set]:
    """
    新版局域网 BAGEL API（client_api/bagel_api.py）健康探测：GET /health 免鉴权。
    - 连接失败/超时 -> (False, "连接失败: ...", set())
    - HTTP 200 且 status=ready/loading/starting -> (True, 详情, capabilities 集合)
    - status=error（模型加载失败）-> (False, 详情, capabilities)
    反馈#10：capabilities 含 transcribe / video_keyframes 等端点能力，供任务路由。
    """
    try:
        resp = requests.get(f"{base_url}/health", timeout=timeout)
        if resp.status_code != 200:
            return True, f"ok (HTTP {resp.status_code})", set()
        d = resp.json()
    except requests.RequestException as e:
        return False, f"连接失败: {type(e).__name__}", set()
    except ValueError:
        return True, "ok (HTTP 200)", set()

    try:
        caps = set(d.get("capabilities") or [])
    except (TypeError, AttributeError):
        caps = set()
    status = d.get("status") or "unknown"
    busy = "推理中" if d.get("busy") else "空闲"
    free = d.get("gpu_free_gib")
    mem = f"，显存空闲 {free}GiB" if isinstance(free, (int, float)) and free >= 0 else ""
    last = d.get("last_event") or ""
    if status == "ready":
        return True, f"模型就绪（{busy}{mem}）", caps
    if status == "error":
        return False, f"模型加载失败：{last}"[:200], caps
    # starting / loading：服务在线但模型仍在加载（约需 3 分钟），任务提交后会由 worker 重试
    return True, f"模型加载中（{status}，{busy}{mem}）", caps


def _autodl_v1_probe(base_url: str, timeout: float) -> Tuple[bool, str, set]:
    """
    autoDL serve_api.py（autodl_v1 协议）健康探测：GET /health 免鉴权。
    返回 JSON：{status, loaded_model, busy, available_models, last_event, gpu_free_gib, ...}
    - 连接失败/超时 -> (False, "连接失败: ...", set())
    - status=ready        -> (True,  "模型就绪（sft_bf16，空闲…）", set())
    - status=loading      -> (True,  "模型加载中…")  服务在线，切换/加载约 10–20s，请求会排队
    - status=waiting_gpu  -> (False, "实验运行中，等待 GPU 空闲…")
        关键：训练/实验在跑时服务端闸门会阻塞推理请求（可能数小时/数天）直到 GPU 空闲，
        若此时派任务，请求会挂到客户端超时再重试，白白占用 worker；故判为不可用，
        提交前预检直接提示、worker 侧快速失败走退避重试。
    - status=error        -> (False, "模型加载失败：…")
    autodl_v1 为 SFT VQA 单图问答服务，无 transcribe/video_keyframes 能力，caps 恒为空集。
    """
    try:
        resp = requests.get(f"{base_url}/health", timeout=timeout)
        if resp.status_code != 200:
            return True, f"ok (HTTP {resp.status_code})", set()
        d = resp.json()
    except requests.RequestException as e:
        return False, f"连接失败: {type(e).__name__}", set()
    except ValueError:
        return True, "ok (HTTP 200)", set()

    status = d.get("status") or "unknown"
    loaded = d.get("loaded_model") or "-"
    busy = "推理中" if d.get("busy") else "空闲"
    free = d.get("gpu_free_gib")
    mem = f"，显存空闲 {free}GiB" if isinstance(free, (int, float)) and free >= 0 else ""
    last = (d.get("last_event") or "")[:120]
    models = d.get("available_models") or []
    model_hint = f"，可用形态 {('/'.join(models))}" if models else ""

    if status == "ready":
        return True, f"模型就绪（已加载 {loaded}，{busy}{mem}）{model_hint}", set()
    if status == "waiting_gpu":
        return False, f"AutoDL 实验运行中，服务等待 GPU 空闲（status=waiting_gpu{model_hint}；{last}）", set()
    if status == "error":
        return False, f"AutoDL 模型服务异常（status=error）：{last}", set()
    # loading / 其他过渡态：服务在线，模型加载/切换中（约 10–20s），请求会在服务端排队
    return True, f"AutoDL 模型加载中（status={status}，{busy}{mem}）", set()


def _gpu_health_info(target: str, cfg, client: Optional[redis.Redis],
                     force_refresh: bool = False) -> Tuple[bool, str, set]:
    """
    探活 GPU 后端并返回 (healthy, detail, capabilities)。
    - 结果在 Redis 缓存 GPU_HEALTH_CACHE_SECONDS（JSON），避免每次提交都探活；
    - client 为 None（离线工具调用）时每次实时探活；
    - infer 协议端点不具备 lan 新端点能力，caps 为空集合。
    """
    target = target if target in GPU_BACKENDS else getattr(cfg, "MODEL_DEFAULT_GPU_BACKEND", BACKEND_LOCAL)
    cache_key = _HEALTH_PREFIX + target
    cache_ttl = int(getattr(cfg, "GPU_HEALTH_CACHE_SECONDS", 60) or 60)

    if client is not None and not force_refresh:
        cached = client.get(cache_key)
        if cached is not None:
            try:
                d = json.loads(str(cached))
                return bool(d.get("ok")), str(d.get("detail") or "ok"), set(d.get("caps") or [])
            except (ValueError, TypeError):
                pass  # 旧版缓存格式（"1"/"0:..."），忽略并重新探活

    url, _ = gpu_endpoint(target, cfg, {})
    proto = gpu_protocol(target, cfg)
    if not url:
        info: Tuple[bool, str, set] = (False, "未配置服务地址", set())
    elif proto == PROTOCOL_LAN:
        # 局域网 NF4 优化版 API（bagel_api.py）：GET /health 免鉴权，返回模型状态与 capabilities
        info = _lan_probe(url, float(getattr(cfg, "BAGEL_LAN_HEALTH_TIMEOUT", 8) or 8))
    elif proto == PROTOCOL_AUTODL_V1:
        # autoDL serve_api.py：GET /health 返回 waiting_gpu/loading/ready/error
        info = _autodl_v1_probe(url, float(getattr(cfg, "BAGEL_AUTODL_HEALTH_TIMEOUT", 8) or 8))
    else:
        try:
            resp = requests.get(f"{url}/health", timeout=float(getattr(cfg, "GPU_HEALTH_TIMEOUT", 5) or 5))
            info = (True, f"ok (HTTP {resp.status_code})", set())
        except requests.RequestException as e:
            info = (False, f"连接失败: {type(e).__name__}", set())

    if client is not None:
        try:
            client.set(cache_key, json.dumps(
                {"ok": info[0], "detail": info[1], "caps": sorted(info[2])},
                ensure_ascii=False), ex=cache_ttl)
        except redis.RedisError:
            pass
    return info


def gpu_backend_health(target: str, cfg, client: Optional[redis.Redis] = None,
                       force_refresh: bool = False) -> Tuple[bool, str]:
    """探活 GPU 后端，返回 (healthy, detail)；capabilities 见 gpu_capabilities()。"""
    ok, detail, _ = _gpu_health_info(target, cfg, client, force_refresh=force_refresh)
    return ok, detail


def gpu_capabilities(target: str, cfg, client: Optional[redis.Redis] = None,
                     force_refresh: bool = False) -> set:
    """返回 lan 端点上报的能力集合（transcribe / video_keyframes / ...）；非 lan 端点为空集。"""
    if gpu_protocol(target, cfg) != PROTOCOL_LAN:
        return set()
    _, _, caps = _gpu_health_info(target, cfg, client, force_refresh=force_refresh)
    return caps


def resolve_capability_target(capability: str, preferred: str, cfg,
                              client: Optional[redis.Redis] = None,
                              force_refresh: bool = False) -> str:
    """
    反馈#10：为 lan 新端点能力（transcribe / video_keyframes）选择可用目标。
    - 仅 lan 协议端点具备这些能力（infer 协议的 autoDL serve_api.py 无对应端点）；
    - 依次尝试 preferred、系统默认 GPU、另一个 GPU 后端：要求已配置地址、健康、capabilities 含该能力；
    - 找不到则抛 FatalBackendError（不可重试，提交前预检与 worker 路由共用）；
    - force_refresh=True 时跳过健康缓存（提交前预检用，worker 路由用缓存）。
    """
    default_gpu = getattr(cfg, "MODEL_DEFAULT_GPU_BACKEND", BACKEND_LOCAL)
    candidates = []
    for t in (preferred, default_gpu, BACKEND_LOCAL, BACKEND_AUTODL):
        if t in GPU_BACKENDS and t not in candidates:
            candidates.append(t)

    details = []
    for t in candidates:
        if gpu_protocol(t, cfg) != PROTOCOL_LAN:
            details.append(f"{t} 为 {gpu_protocol(t, cfg)} 协议端点（无 {capability} 能力）")
            continue
        url, _ = gpu_endpoint(t, cfg, {})
        if not url:
            details.append(f"{t} 未配置服务地址")
            continue
        ok, detail, caps = _gpu_health_info(t, cfg, client, force_refresh=force_refresh)
        if not ok:
            details.append(f"{t} 不可用：{detail}")
            continue
        if capability in caps:
            return t
        details.append(f"{t} 在线但不支持 {capability}（能力列表：{sorted(caps) or '无'}，"
                       "请更新 bagel_api.py 并安装 ffmpeg/faster-whisper 依赖）")
    raise FatalBackendError(
        f"没有可用的 GPU 端点支持「{capability}」能力：" + "；".join(details) + "。"
        "请在局域网 GPU 机更新 client_api/bagel_api.py、安装 ffmpeg 与 faster-whisper，"
        "并确认网络隧道已连通。"
    )


def resolve_gpu_target(preferred: str, cfg, client: Optional[redis.Redis] = None) -> Tuple[str, Optional[str]]:
    """
    选择实际调用的 GPU 目标：
    - preferred 健康 → 直接用；
    - preferred 不健康且开启故障切换、另一个 GPU 后端已配置且健康 → 切换，返回 (target, failover_from)；
    - 两边都不可用 → 返回 preferred（交由调用方报错/重试），failover_from=None。
    """
    preferred = preferred if preferred in GPU_BACKENDS else getattr(cfg, "MODEL_DEFAULT_GPU_BACKEND", BACKEND_LOCAL)
    ok, _ = gpu_backend_health(preferred, cfg, client)
    if ok:
        return preferred, None
    if not getattr(cfg, "GPU_FAILOVER_ENABLED", True):
        return preferred, None

    other = BACKEND_LOCAL if preferred == BACKEND_AUTODL else BACKEND_AUTODL
    other_url, _ = gpu_endpoint(other, cfg, {})
    if not other_url:
        return preferred, None
    ok2, _ = gpu_backend_health(other, cfg, client)
    if ok2:
        return other, preferred
    return preferred, None


def resolve_capable_gpu_target(preferred: str, payload: Dict[str, Any], cfg,
                               client: Optional[redis.Redis] = None) -> Tuple[str, Optional[str]]:
    """
    在 resolve_gpu_target（健康故障切换）之上增加【协议能力感知路由】：

    图片-only 协议端点（lan 局域网 BAGEL NF4 优化版、autodl_v1 autoDL SFT VQA 服务）
    【仅支持图片理解】。音频/视频任务即使该端点健康，也无法处理——此时若另一个 GPU 端点
    已配置，则自动改路由过去；未配置则保持原目标，由调用方抛出明确的 FatalBackendError
    （入死信不重试），避免无意义重试。

    返回 (target, failover_from)。
    """
    target, failover_from = resolve_gpu_target(preferred, cfg, client)
    modality = (payload.get("modality") or "").strip().lower()
    if modality in LAN_SUPPORTED_MODALITIES or gpu_protocol(target, cfg) not in IMAGE_ONLY_PROTOCOLS:
        return target, failover_from

    # 非图片任务落到了仅支持图片的端点（lan / autodl_v1）：
    # 仅当另一个端点是【全模态协议】（旧 infer）且已配置地址时才改路由；
    # 若另一端同样是图片-only（lan/autodl_v1），切换无意义（音视频理解应改走「AI 转写」任务，
    # 由提交前预检拦截），保持原目标由调用层抛 FatalBackendError，避免两端来回弹跳。
    other = BACKEND_LOCAL if target == BACKEND_AUTODL else BACKEND_AUTODL
    if gpu_protocol(other, cfg) in IMAGE_ONLY_PROTOCOLS:
        return target, failover_from
    other_url, _ = gpu_endpoint(other, cfg, payload)
    if other_url:
        return other, (failover_from or target)
    return target, failover_from


# ---------------- DeepSeek 配额 ----------------

def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m")


def deepseek_quota_status(client: redis.Redis, cfg) -> Dict[str, Any]:
    """返回当日/当月 token 已用量与上限（limit=0 表示不限）。"""
    daily_used = int(client.get(_QUOTA_DAILY_PREFIX + _today_key()) or 0)
    monthly_used = int(client.get(_QUOTA_MONTHLY_PREFIX + _month_key()) or 0)
    return {
        "daily_used": daily_used,
        "daily_limit": cfg.DEEPSEEK_DAILY_TOKEN_LIMIT,
        "monthly_used": monthly_used,
        "monthly_limit": cfg.DEEPSEEK_MONTHLY_TOKEN_LIMIT,
    }


def _check_quota(client: redis.Redis, cfg) -> None:
    """配额预检：已用量达到上限即拒绝（limit=0 不限制）。"""
    if not cfg.DEEPSEEK_API_KEY:
        raise FatalBackendError("DEEPSEEK_API_KEY 未配置")
    st = deepseek_quota_status(client, cfg)
    if cfg.DEEPSEEK_DAILY_TOKEN_LIMIT and st["daily_used"] >= cfg.DEEPSEEK_DAILY_TOKEN_LIMIT:
        raise QuotaExceeded(
            f"DeepSeek 当日 token 配额已用尽（{st['daily_used']}/{cfg.DEEPSEEK_DAILY_TOKEN_LIMIT}），请次日再试或联系管理员"
        )
    if cfg.DEEPSEEK_MONTHLY_TOKEN_LIMIT and st["monthly_used"] >= cfg.DEEPSEEK_MONTHLY_TOKEN_LIMIT:
        raise QuotaExceeded(
            f"DeepSeek 当月 token 配额已用尽（{st['monthly_used']}/{cfg.DEEPSEEK_MONTHLY_TOKEN_LIMIT}），请联系管理员调整上限"
        )


def _record_usage(client: redis.Redis, total_tokens: int) -> None:
    """调用成功后累计 token 用量（日键保留40天，月键保留370天）。"""
    if total_tokens <= 0:
        return
    dk = _QUOTA_DAILY_PREFIX + _today_key()
    mk = _QUOTA_MONTHLY_PREFIX + _month_key()
    pipe = client.pipeline()
    pipe.incrby(dk, total_tokens).expire(dk, 40 * 86400)
    pipe.incrby(mk, total_tokens).expire(mk, 370 * 86400)
    pipe.execute()


# ---------------- 文件传输 ----------------

def build_file_download_url(payload: Dict[str, Any], cfg, with_token: bool = False) -> Optional[str]:
    """
    为外部 worker（autoDL/局域网）构造云端文件下载地址。
    依赖 Web 端 /model-files/<filename> 路由与 MODEL_FILE_TOKEN 共享密钥。

    f4 安全：默认返回【不带 Token】的干净 URL（Token 不进入 URL/访问日志/Redis 明文），
    由下载方通过 Authorization: Bearer 头鉴权；仅 BAGEL_FILE_TRANSFER=url 模式
    （GPU 推理服务自行回拉、无法附加请求头）才以 with_token=True 追加 ?token=。
    """
    if not cfg.MODEL_FILE_BASE_URL or not cfg.MODEL_FILE_TOKEN:
        return None
    filename = payload.get("filename") or os.path.basename(payload.get("file_path") or "")
    if not filename:
        return None
    url = f"{cfg.MODEL_FILE_BASE_URL}/model-files/{quote(filename)}"
    if with_token:
        url = f"{url}?token={cfg.MODEL_FILE_TOKEN}"
    return url


def _model_file_auth_headers(cfg) -> Dict[str, str]:
    token = getattr(cfg, "MODEL_FILE_TOKEN", "") or ""
    return {"Authorization": f"Bearer {token}"} if token else {}


def _ensure_local_file(payload: Dict[str, Any], uploads_dir: str, cfg=None) -> Tuple[Optional[str], bool]:
    """
    返回 (可读取的本地文件路径, 是否为临时下载文件)：
    - 云端 worker：file_path 指向容器内 /app/uploads，直接可用（is_temp=False）
    - 外部 worker（autoDL/局域网）：本地无文件时，凭 file_download_url 下载到临时目录
      （is_temp=True，调用方须在用完后删除，避免 /tmp 临时文件泄漏）
    f4：下载鉴权优先走 Authorization: Bearer 头（Token 不入 URL/日志）。
    """
    file_path = payload.get("file_path") or ""
    if file_path and os.path.exists(file_path):
        return file_path, False

    # uploads 目录内按文件名查找
    filename = payload.get("filename") or os.path.basename(file_path)
    if filename:
        candidate = os.path.join(uploads_dir, filename)
        if os.path.exists(candidate):
            return candidate, False

    # 外部 worker：从云端下载（带 Bearer 鉴权头）
    url = payload.get("file_download_url")
    if url:
        resp = requests.get(url, headers=_model_file_auth_headers(cfg), timeout=600)
        _raise_for_status(resp)
        suffix = os.path.splitext(filename)[1] if filename else ""
        fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(resp.content)
        return tmp_path, True
    return None, False


# ---------------- 后端调用 ----------------

def _encode_image_base64(local_file: str) -> str:
    """
    读取图片并转 base64（适配 GPU API 的 40MB 请求体上限，base64 膨胀约 4/3）：
    原图 ≤28MB 直接编码；超限则用 Pillow 等比缩到最长边 2048px、JPEG q88 后编码。
    lan 与 autodl_v1 两个 JSON+base64 协议共用。
    """
    with open(local_file, "rb") as f:
        raw = f.read()
    if len(raw) <= LAN_MAX_IMAGE_BYTES:
        return base64.b64encode(raw).decode("ascii")

    # 大图压缩（Pillow 为可选依赖；缺失时退回原始 base64，由服务端 413 兜底）
    try:
        from PIL import Image
        import io as _io

        with Image.open(_io.BytesIO(raw)) as im:
            im = im.convert("RGB")
            im.thumbnail((2048, 2048))
            buf = _io.BytesIO()
            im.save(buf, format="JPEG", quality=88)
            return base64.b64encode(buf.getvalue()).decode("ascii")
    except ImportError:
        return base64.b64encode(raw).decode("ascii")


def call_bagel_lan(payload: Dict[str, Any], cfg, uploads_dir: str, target: str) -> Dict[str, Any]:
    """
    调用新版局域网 BAGEL API（client_api/bagel_api.py）：POST /v1/understand。
    - 协议：Bearer 鉴权 + JSON（prompt + image_base64）；
    - 能力：NF4 优化版【仅支持图片理解】（音视频须走 autoDL 满血版，由 dispatch 路由）；
    - 模型加载中/排队时服务端返回 503 + Retry-After，按可重试错误处理（worker 退避重试）。
    """
    modality = (payload.get("modality") or "").strip().lower()
    if modality not in LAN_SUPPORTED_MODALITIES:
        raise FatalBackendError(
            f"局域网 BAGEL 优化版（NF4）仅支持图片理解，不支持 {modality or '该'} 类型；"
            "音视频算法任务请选择 autoDL 满血版（POST /infer 协议）"
        )

    base_url, service_token = gpu_endpoint(target, cfg, payload)
    if not base_url:
        raise FatalBackendError(f"GPU 后端 {target} 未配置服务地址（请设置 BAGEL_{target.upper()}_SERVICE_URL）")

    local_file, is_temp = _ensure_local_file(payload, uploads_dir, cfg)
    if not local_file:
        raise FatalBackendError(
            "文件不可用：云端 worker 请检查 uploads 挂载；外部 worker 请配置 MODEL_FILE_BASE_URL/MODEL_FILE_TOKEN"
        )
    try:
        image_b64 = _encode_image_base64(local_file)
    finally:
        if is_temp:
            try:
                os.remove(local_file)
            except OSError:
                pass

    options = payload.get("options") or {}
    prompt = (options.get("prompt") or "").strip()
    if not prompt:
        prompt = DEFAULT_BAGEL_IMAGE_PROMPT
    extra = (options.get("prompt_extra") or "").strip()
    if extra:
        prompt = f"{prompt}\n\n【附加要求】{extra}"

    body = {
        "prompt": prompt,
        "image_base64": image_b64,
        "do_sample": False,
        "temperature": float(options.get("temperature", 0.3) or 0.3),
        "max_new_tokens": int(options.get("max_new_tokens", 1024) or 1024),
    }
    headers = {"Content-Type": "application/json"}
    if service_token:
        headers["Authorization"] = f"Bearer {service_token}"

    resp = requests.post(
        f"{base_url}/v1/understand",
        json=body,
        headers=headers,
        timeout=float(getattr(cfg, "BAGEL_LAN_UNDERSTAND_TIMEOUT", 300) or 300),
    )
    _raise_for_status(resp)  # 4xx（含 401/413）→ FatalBackendError；503/5xx → 可重试
    data = resp.json()
    text = (data.get("response") or "").strip()
    if not text:
        raise BackendError(f"局域网 BAGEL 返回为空或格式异常: {str(data)[:200]}")
    return {
        "content_text": text,
        "timeline": [],          # 图片描述无时间轴
        "backend": target,
        "protocol": PROTOCOL_LAN,
        "model": data.get("model"),
        "infer_sec": data.get("infer_sec"),
    }


def call_bagel_autodl_v1(payload: Dict[str, Any], cfg, uploads_dir: str, target: str) -> Dict[str, Any]:
    """
    调用 autoDL serve_api.py（autodl_v1 协议）：POST /v1/infer，JSON + base64 图片。
    - 契约（见 autodl_api/BAGEL模型API服务设计-20260902.md）：
      请求 {model, prompt, image_base64, do_sample, temperature, max_new_tokens}；
      返回 {model, response, infer_sec}；鉴权 Authorization: Bearer <key>。
    - 能力：SFT VQA 单图问答（sft_nf4 常驻省显存 / sft_bf16 满血，服务端自动切换形态）；
      不支持音/视频（此类任务应走 lan 端点 Whisper 转写编排）。
    - 训练期保护：/health 为 waiting_gpu 时服务端闸门会阻塞请求（可能数小时），
      此处先做健康快检，不可用即抛【可重试】错误让 worker 退避重试，避免挂死到超时。
    """
    modality = (payload.get("modality") or "").strip().lower()
    if modality not in LAN_SUPPORTED_MODALITIES:
        raise FatalBackendError(
            f"autoDL SFT 模型服务（serve_api.py）仅支持单图理解，不支持 {modality or '该'} 类型；"
            "音/视频任务请使用「AI 转写」（Whisper，局域网 BAGEL 优化版端点）"
        )

    base_url, service_token = gpu_endpoint(target, cfg, payload)
    if not base_url:
        raise FatalBackendError(f"GPU 后端 {target} 未配置服务地址（请设置 BAGEL_{target.upper()}_SERVICE_URL）")

    # 训练期/异常快检（健康结果有 Redis 缓存，不会每次请求都探活）：
    # waiting_gpu（实验运行中）/ error / 连接失败 -> 快速失败走退避重试，不向服务端发推理请求
    ok, detail, _ = _gpu_health_info(target, cfg, None)
    if not ok:
        raise BackendError(f"autoDL 模型服务暂不可用（{detail}），将按退避策略重试")

    local_file, is_temp = _ensure_local_file(payload, uploads_dir, cfg)
    if not local_file:
        raise FatalBackendError(
            "文件不可用：云端 worker 请检查 uploads 挂载；外部 worker 请配置 MODEL_FILE_BASE_URL/MODEL_FILE_TOKEN"
        )
    try:
        image_b64 = _encode_image_base64(local_file)
    finally:
        if is_temp:
            try:
                os.remove(local_file)
            except OSError:
                pass

    options = payload.get("options") or {}
    prompt = (options.get("prompt") or "").strip()
    if not prompt:
        prompt = DEFAULT_BAGEL_IMAGE_PROMPT
    extra = (options.get("prompt_extra") or "").strip()
    if extra:
        prompt = f"{prompt}\n\n【附加要求】{extra}"

    # 模型形态：sft_nf4（省显存、加载快）/ sft_bf16（满血精度）；
    # 可用任务级 options.autodl_model 覆盖，缺省取 BAGEL_AUTODL_MODEL（默认 sft_nf4）
    model_form = (options.get("autodl_model") or getattr(cfg, "BAGEL_AUTODL_MODEL", "") or "sft_nf4").strip()
    body = {
        "model": model_form,
        "prompt": prompt,
        "image_base64": image_b64,
        "do_sample": False,
        "temperature": float(options.get("temperature", 0.3) or 0.3),
        "max_new_tokens": int(options.get("max_new_tokens", 1024) or 1024),
    }
    headers = {"Content-Type": "application/json"}
    if service_token:
        headers["Authorization"] = f"Bearer {service_token}"

    resp = requests.post(
        f"{base_url}/v1/infer",
        json=body,
        headers=headers,
        timeout=float(getattr(cfg, "BAGEL_AUTODL_V1_TIMEOUT", 300) or 300),
    )
    _raise_for_status(resp)  # 4xx（含 401/400/413）→ FatalBackendError；5xx/网络错误 → 可重试
    data = resp.json()
    text = (data.get("response") or "").strip()
    if not text:
        raise BackendError(f"autoDL SFT 返回为空或格式异常: {str(data)[:200]}")
    return {
        "content_text": text,
        "timeline": [],          # 图片描述无时间轴
        "backend": target,
        "protocol": PROTOCOL_AUTODL_V1,
        "model": data.get("model") or model_form,
        "infer_sec": data.get("infer_sec"),
    }


def call_bagel(payload: Dict[str, Any], cfg, uploads_dir: str, target: str = "") -> Dict[str, Any]:
    """
    调用自建 GPU 推理服务。按后端协议分流：
    - lan（局域网 NF4 优化版，client_api/bagel_api.py）：POST /v1/understand，仅图片理解，见 call_bagel_lan；
    - autodl_v1（autoDL 2026-09 版 serve_api.py）：POST /v1/infer（JSON+base64），SFT 单图问答，见 call_bagel_autodl_v1；
    - infer（旧版 BAGEL 协议，POST /infer）：multipart/url 推送文件，全模态（仅旧服务兼容）。
    target: autodl（满血版）/ local（优化版）；为空时按默认 GPU 端点。
    """
    target = target if target in GPU_BACKENDS else getattr(cfg, "MODEL_DEFAULT_GPU_BACKEND", BACKEND_LOCAL)
    proto = gpu_protocol(target, cfg)
    if proto == PROTOCOL_LAN:
        return call_bagel_lan(payload, cfg, uploads_dir, target)
    if proto == PROTOCOL_AUTODL_V1:
        return call_bagel_autodl_v1(payload, cfg, uploads_dir, target)

    bagel_url, service_token = gpu_endpoint(target, cfg, payload)
    if not bagel_url:
        raise FatalBackendError(f"GPU 后端 {target} 未配置服务地址（请设置 BAGEL_{target.upper()}_SERVICE_URL）")
    endpoint = f"{bagel_url}/infer"
    headers = {}
    if service_token:
        headers["Authorization"] = f"Bearer {service_token}"

    options = payload.get("options") or {}
    if cfg.BAGEL_FILE_TRANSFER == "url":
        # url 模式由 GPU 推理服务自行回拉（无法附加鉴权头），使用带 ?token= 的 URL
        pull_url = build_file_download_url(payload, cfg, with_token=True) or payload.get("file_download_url")
        body = {
            "file_path": payload.get("file_path"),
            "filename": payload.get("filename"),
            "file_download_url": pull_url,
            "modality": payload.get("modality"),
            "options": options,
        }
        resp = requests.post(endpoint, json=body, headers=headers, timeout=1800)
    else:
        local_file, is_temp = _ensure_local_file(payload, uploads_dir, cfg)
        if not local_file:
            raise FatalBackendError(
                "文件不可用：云端 worker 请检查 uploads 挂载；外部 worker 请配置 MODEL_FILE_BASE_URL/MODEL_FILE_TOKEN"
            )
        try:
            with open(local_file, "rb") as f:
                files = {"file": (payload.get("filename") or os.path.basename(local_file), f)}
                data = {"options": json.dumps(options, ensure_ascii=False), "modality": payload.get("modality") or ""}
                resp = requests.post(endpoint, files=files, data=data, headers=headers, timeout=1800)
        finally:
            # 外部 worker 下载的临时文件即用即删，防止 /tmp 泄漏（uploads 内原文件不删）
            if is_temp:
                try:
                    os.remove(local_file)
                except OSError:
                    pass
    _raise_for_status(resp)
    return resp.json()


# ---------------- 反馈#10：Whisper 转写 / 视频关键帧简述（lan 新端点） ----------------

def _normalize_segments(segs: Any) -> list:
    """
    Whisper 返回的分段（start/end 为浮点秒）规整为时间轴编辑器契约：
    [{start: int 秒, end: int 秒, text: str}]；end 必须晚于 start（不足 1 秒补 1 秒）。
    """
    out = []
    for s in segs or []:
        if not isinstance(s, dict):
            continue
        text = str(s.get("text") or "").strip()
        if not text:
            continue
        try:
            start = int(round(float(s.get("start") or 0)))
            end = int(round(float(s.get("end") or 0)))
        except (TypeError, ValueError):
            continue
        start = max(0, start)
        if end <= start:
            end = start + 1
        out.append({"start": start, "end": end, "text": text})
    return out


def _call_lan_media_file(payload: Dict[str, Any], cfg, uploads_dir: str, target: str,
                         endpoint: str, fields: Dict[str, str], timeout: float) -> Dict[str, Any]:
    """
    以 multipart/form-data 上传媒体文件到局域网 API 新端点（/v1/transcribe、/v1/video-keyframes）。
    - 文件来源复用 _ensure_local_file（云端 worker 直读 uploads；外部 worker 凭下载 URL 拉取）；
    - 文件大小超 BAGEL_LAN_MEDIA_MAX_MB 时抛 FatalBackendError（重试无意义）；
    - 503（Whisper/BAGEL 模型加载中或忙）/5xx 为可重试错误，4xx 为 FatalBackendError。
    """
    base_url, service_token = gpu_endpoint(target, cfg, payload)
    if not base_url:
        raise FatalBackendError(f"GPU 后端 {target} 未配置服务地址（请设置 BAGEL_{target.upper()}_SERVICE_URL）")

    local_file, is_temp = _ensure_local_file(payload, uploads_dir, cfg)
    if not local_file:
        raise FatalBackendError(
            "文件不可用：云端 worker 请检查 uploads 挂载；外部 worker 请配置 MODEL_FILE_BASE_URL/MODEL_FILE_TOKEN"
        )
    try:
        size_mb = os.path.getsize(local_file) / 1024 / 1024
        limit_mb = int(getattr(cfg, "BAGEL_LAN_MEDIA_MAX_MB", 300) or 300)
        if size_mb > limit_mb:
            raise FatalBackendError(
                f"媒体文件 {size_mb:.1f}MB 超过局域网 API 上传上限 {limit_mb}MB"
                "（请在 .env 调大 BAGEL_LAN_MEDIA_MAX_MB 与 GPU 机 BAGEL_MEDIA_MAX_BODY_MB，或改用较短的素材）"
            )
        headers = {}
        if service_token:
            headers["Authorization"] = f"Bearer {service_token}"
        with open(local_file, "rb") as f:
            files = {"file": (payload.get("filename") or os.path.basename(local_file), f)}
            resp = requests.post(
                f"{base_url}{endpoint}", files=files, data=fields,
                headers=headers, timeout=timeout,
            )
        _raise_for_status(resp)
        return resp.json()
    finally:
        if is_temp:
            try:
                os.remove(local_file)
            except OSError:
                pass


def call_whisper_transcribe(payload: Dict[str, Any], cfg, uploads_dir: str, target: str) -> Dict[str, Any]:
    """调用局域网 API /v1/transcribe：Whisper 转写音/视频，返回带时间轴的分段草稿。"""
    options = payload.get("options") or {}
    fields = {
        "language": str(options.get("whisper_language") or options.get("language") or ""),
        "beam_size": str(int(options.get("beam_size") or 5)),
        "vad_filter": str(bool(options.get("vad_filter", True))).lower(),
    }
    timeout = float(getattr(cfg, "BAGEL_LAN_TRANSCRIBE_TIMEOUT", 1800) or 1800)
    data = _call_lan_media_file(payload, cfg, uploads_dir, target, "/v1/transcribe", fields, timeout)

    segments = _normalize_segments(data.get("segments"))
    text = (data.get("text") or "").strip() or "\n".join(s["text"] for s in segments)
    if not segments and not text:
        raise BackendError("Whisper 转写结果为空（可能是静音/无人声片段），可稍后重试或手工转写")
    return {
        "content_text": text,
        "timeline": segments,
        "backend": target,
        "protocol": PROTOCOL_LAN,
        "transcribe_engine": "whisper",
        "model": data.get("model"),
        "infer_sec": data.get("infer_sec"),
        "duration_sec": data.get("duration"),
        "language": data.get("language"),
    }


def call_video_keyframes(payload: Dict[str, Any], cfg, uploads_dir: str, target: str) -> Dict[str, Any]:
    """调用局域网 API /v1/video-keyframes：ffmpeg 抽帧 + BAGEL 逐帧描述，汇总为视频主要内容简述。"""
    options = payload.get("options") or {}
    max_frames = int(options.get("keyframe_max_frames") or getattr(cfg, "CREDIT_KEYFRAME_MAX_FRAMES", 6) or 6)
    fields = {
        "max_frames": str(max(1, min(12, max_frames))),
        "prompt": str(options.get("keyframe_prompt") or DEFAULT_KEYFRAME_PROMPT),
    }
    timeout = float(getattr(cfg, "BAGEL_LAN_KEYFRAMES_TIMEOUT", 1800) or 1800)
    data = _call_lan_media_file(payload, cfg, uploads_dir, target, "/v1/video-keyframes", fields, timeout)

    summary = (data.get("summary") or "").strip()
    keyframes = []
    for k in data.get("keyframes") or []:
        if isinstance(k, dict) and (k.get("description") or "").strip():
            keyframes.append({"t": k.get("t"), "description": str(k.get("description") or "").strip()})
    if not summary and not keyframes:
        raise BackendError("关键帧简述结果为空（抽帧或 BAGEL 描述失败），可稍后重试")
    return {
        "video_summary": summary or "\n".join(
            f"[{k.get('t')}s] {k.get('description')}" for k in keyframes),
        "keyframes": keyframes,
        "keyframe_count": data.get("frame_count") or len(keyframes),
        "keyframe_infer_sec": data.get("infer_sec"),
        "keyframe_model": data.get("model"),
        "duration_sec": data.get("duration"),
    }


def dispatch_transcribe(payload: Dict[str, Any], cfg, client: redis.Redis, uploads_dir: str) -> Tuple[str, Dict[str, Any]]:
    """
    反馈#10：Whisper 转写任务编排（在 worker 中执行）。
    1) Whisper 转写（lan 端点 transcribe 能力）→ 时间轴分段草稿；
    2) options.with_keyframes（仅视频）：BAGEL 关键帧简述（lan 端点 video_keyframes 能力）；
    3) options.with_llm_summary（仅视频）：以转写文本+关键帧简述调外部大模型生成详细视频描述；
       外部大模型失败不影响转写主结果（降级记录 llm_summary_error，不计该部分积分）。
    返回 (主后端, result)。
    """
    options = payload.get("options") or {}
    modality = (payload.get("modality") or "").strip().lower()
    if modality not in ("audio", "video"):
        raise FatalBackendError(f"Whisper 转写仅支持音频/视频文件（当前模态：{modality or '未知'}）")

    preferred = select_backend(
        payload, cfg.MODEL_DEFAULT_BACKEND,
        getattr(cfg, "MODEL_DEFAULT_GPU_BACKEND", BACKEND_LOCAL),
    )
    if preferred == BACKEND_DEEPSEEK:
        preferred = getattr(cfg, "MODEL_DEFAULT_GPU_BACKEND", BACKEND_LOCAL)

    # 1) Whisper 转写
    target = resolve_capability_target(CAP_TRANSCRIBE, preferred, cfg, client)
    result = call_whisper_transcribe(payload, cfg, uploads_dir, target)

    # 2) 可选：BAGEL 关键帧简述（仅视频）
    if options.get("with_keyframes") and modality == "video":
        kf_target = resolve_capability_target(CAP_VIDEO_KEYFRAMES, target, cfg, client)
        kf = call_video_keyframes(payload, cfg, uploads_dir, kf_target)
        result["video_summary"] = kf["video_summary"]
        result["keyframes"] = kf["keyframes"]
        result["keyframe_count"] = kf["keyframe_count"]
        result["keyframe_infer_sec"] = kf.get("keyframe_infer_sec")
        if kf_target != target:
            result["keyframe_backend"] = kf_target
        if not result.get("duration_sec"):
            result["duration_sec"] = kf.get("duration_sec")

    # 3) 可选：外部大模型详细视频描述（转写文本 + 关键帧简述 → DeepSeek）
    if options.get("with_llm_summary") and modality == "video":
        try:
            from services import algorithm_prompts
            llm_payload = dict(payload)
            llm_payload["backend"] = BACKEND_DEEPSEEK
            llm_options = dict(options)
            llm_options.update({
                "system_prompt": algorithm_prompts.DEEPSEEK_VIDEO_SUMMARY_SYSTEM_PROMPT,
                "prompt": algorithm_prompts.build_video_summary_prompt(
                    transcript=result.get("content_text") or "",
                    keyframe_summary=result.get("video_summary") or "",
                    duration=result.get("duration_sec") or 0,
                    filename=payload.get("filename") or "",
                ),
                "json_mode": False,
                "temperature": 0.3,
            })
            llm_payload["options"] = llm_options
            llm_res = call_deepseek(llm_payload, cfg, client)
            summary_text = ""
            if isinstance(llm_res, dict):
                summary_text = (llm_res.get("video_description")
                                or llm_res.get("content_text")
                                or llm_res.get("response") or "")
                tokens = int(llm_res.get("tokens_used") or 0)
                if tokens:
                    result["tokens_used"] = int(result.get("tokens_used") or 0) + tokens
            result["llm_summary"] = str(summary_text or "").strip()
            if not result["llm_summary"]:
                result["llm_summary_error"] = "外部大模型返回为空"
        except Exception as e:
            # 降级：转写/简述已成功，大模型详述失败不置任务失败（该部分不计积分）
            result["llm_summary_error"] = f"{type(e).__name__}: {e}"[:200]

    result["backend"] = target
    result.setdefault("protocol", PROTOCOL_LAN)
    return target, result


def call_deepseek(payload: Dict[str, Any], cfg, client: redis.Redis) -> Dict[str, Any]:
    """
    调用 DeepSeek（OpenAI 兼容 chat/completions），执行配额预检与用量记录。
    适用纯文本任务：字幕校对、内容分析、标签/摘要生成等（options.prompt 提供指令）。
    """
    _check_quota(client, cfg)

    options = payload.get("options") or {}
    prompt = options.get("prompt") or payload.get("text") or ""
    if not prompt:
        raise FatalBackendError("deepseek 后端需要 options.prompt 或 text 输入")

    messages = options.get("messages")
    if not messages:
        system_prompt = options.get("system_prompt") or "你是 MAPS 多模态数据处理平台的算法助手，请根据用户输入完成处理并返回 JSON。"
        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}]

    body = {
        "model": options.get("model") or cfg.DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": options.get("temperature", 0.2),
        "response_format": {"type": "json_object"} if options.get("json_mode", True) else None,
    }
    body = {k: v for k, v in body.items() if v is not None}

    resp = requests.post(
        f"{cfg.DEEPSEEK_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {cfg.DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
        json=body,
        timeout=cfg.DEEPSEEK_REQUEST_TIMEOUT,
    )
    _raise_for_status(resp)
    data = resp.json()

    # 配额记账：优先使用 API 返回的 usage.total_tokens
    total_tokens = (data.get("usage") or {}).get("total_tokens") or _estimate_tokens(messages)
    _record_usage(client, int(total_tokens))

    content = ""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise BackendError(f"DeepSeek 返回格式异常: {data}")

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        parsed = {"content_text": content}
    parsed.setdefault("backend", BACKEND_DEEPSEEK)
    parsed.setdefault("tokens_used", total_tokens)
    return parsed


def _estimate_tokens(messages) -> int:
    """无 usage 字段时的粗估（中文约1.5字/token，保守按字符数计）。"""
    chars = sum(len(m.get("content") or "") for m in messages)
    return max(1, chars)


def dispatch(payload: Dict[str, Any], cfg, client: redis.Redis, uploads_dir: str) -> Tuple[str, Dict[str, Any]]:
    """
    统一入口：路由 -> (GPU 健康检查/故障切换) -> 调用 -> 返回 (backend, result)。
    backend 为实际使用的后端：autodl / local / deepseek 之一。
    GPU 任务在首选端点不健康时自动切换到另一个健康端点（result.failover_from 记录来源）。
    反馈#10：options.task_kind='transcribe' 走 Whisper 转写编排（含可选关键帧/外部大模型）。
    """
    options = payload.get("options") or {}
    kind = str(options.get("task_kind") or TASK_KIND_UNDERSTAND).strip().lower()
    if kind == TASK_KIND_TRANSCRIBE:
        return dispatch_transcribe(payload, cfg, client, uploads_dir)

    backend = select_backend(
        payload,
        cfg.MODEL_DEFAULT_BACKEND,
        getattr(cfg, "MODEL_DEFAULT_GPU_BACKEND", BACKEND_LOCAL),
    )
    if backend == BACKEND_DEEPSEEK:
        return backend, call_deepseek(payload, cfg, client)

    target, failover_from = resolve_capable_gpu_target(backend, payload, cfg, client)
    result = call_bagel(payload, cfg, uploads_dir, target=target)
    if isinstance(result, dict):
        result.setdefault("backend", target)
        if failover_from:
            result["failover_from"] = failover_from
    return target, result
