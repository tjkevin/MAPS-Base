"""
按人算力积分服务（Web 侧）
==========================
设计要点：
- 管理员按人发放积分（user_credit_grants 追加记录为持久凭据，Redis 余额实时累加）；
- 提交算法任务时按后端/媒体时长预估并**冻结**积分（Redis Lua 原子扣减，防并发超支）；
- 任务完成后按实际用量**结算**（DeepSeek 按 token、GPU 按媒体时长 + 后端权重），多退少补；
  任务失败全额退还；同文件(MD5)结果复用免积分；
- 用量流水持久化到 compute_usage_logs（MySQL 账单），Redis 余额丢失可由流水重建。

注意：外部 worker（autoDL/局域网）只连 Redis、不连 MySQL，因此所有 DB 记账都在
Web 侧完成（提交时冻结、状态轮询/确认时结算）。
"""
import math
from typing import Any, Dict, Optional, Tuple

import redis
from sqlalchemy import func

from models import db, User, Recording, UserCreditGrant, ComputeUsageLog
from services.model_backends import (
    BACKEND_AUTODL,
    BACKEND_LOCAL,
    BACKEND_DEEPSEEK,
    select_backend,
)

_BAL_PREFIX = "credit:balance:"

# 原子冻结：余额不足返回 -1，否则扣减并返回剩余余额
_FREEZE_LUA = """
local bal = tonumber(redis.call('GET', KEYS[1]) or '0')
local cost = tonumber(ARGV[1])
if bal < cost then
  return -1
end
return redis.call('DECRBY', KEYS[1], cost)
"""


class CreditInsufficient(RuntimeError):
    """积分余额不足。"""

    def __init__(self, message: str, balance: int = 0, need: int = 0):
        super().__init__(message)
        self.balance = balance
        self.need = need


def balance_key(user_id: int) -> str:
    return f"{_BAL_PREFIX}{user_id}"


# ---------------- 定价 ----------------

def _gpu_pricing(backend: str, cfg) -> Tuple[int, float]:
    """返回 (基础积分/任务, 每分钟积分)。"""
    if backend == BACKEND_AUTODL:
        return int(cfg.CREDIT_AUTODL_BASE), float(cfg.CREDIT_AUTODL_PER_MIN)
    return int(cfg.CREDIT_LOCAL_BASE), float(cfg.CREDIT_LOCAL_PER_MIN)


def _media_seconds(recording: Optional[Recording], payload: Dict[str, Any], cfg) -> float:
    """GPU 计费时长：优先取录制真实时长，缺失时按模态保守预估。"""
    if recording is not None and recording.duration:
        return float(recording.duration)
    modality = (payload or {}).get("modality") or ""
    if modality == "audio":
        return float(cfg.CREDIT_EST_AUDIO_SECONDS)
    if modality == "video":
        return float(cfg.CREDIT_EST_VIDEO_SECONDS)
    return 0.0  # 图片/未知：仅收基础积分


def _task_kind(payload: Dict[str, Any]) -> str:
    options = (payload or {}).get("options") or {}
    return str(options.get("task_kind") or "understand").strip().lower()


def _est_keyframe_count(seconds: float, cfg) -> int:
    """关键帧预估帧数：按抽帧间隔与时长估算，封顶 CREDIT_KEYFRAME_MAX_FRAMES。"""
    maxf = int(getattr(cfg, "CREDIT_KEYFRAME_MAX_FRAMES", 6) or 6)
    interval = float(getattr(cfg, "CREDIT_KEYFRAME_INTERVAL_SEC", 8) or 8)
    if seconds <= 0:
        return maxf
    return max(1, min(maxf, int(math.ceil(seconds / interval))))


def _transcribe_estimate(recording: Optional[Recording], payload: Dict[str, Any],
                         cfg, seconds: float) -> int:
    """
    反馈#10：Whisper 转写任务预估积分 =
      转写基础+时长积分 [+ 关键帧简述基础+按帧积分（视频且勾选）] [+ 外部大模型详述积分（视频且勾选）]。
    """
    options = (payload or {}).get("options") or {}
    credits = int(cfg.CREDIT_TRANSCRIBE_BASE) + int(math.ceil(
        seconds / 60.0 * float(cfg.CREDIT_TRANSCRIBE_PER_MIN)))
    frames = 0
    if options.get("with_keyframes") and (payload or {}).get("modality") == "video":
        frames = _est_keyframe_count(seconds, cfg)
        credits += int(cfg.CREDIT_KEYFRAMES_BASE) + int(math.ceil(
            frames * float(cfg.CREDIT_KEYFRAMES_PER_FRAME)))
    if options.get("with_llm_summary") and (payload or {}).get("modality") == "video":
        # 外部大模型：粗估输入（转写约 4 字/秒 + 每帧描述约 150 字）+ 输出约 800 字，按 1 字≈1 token
        est_chars = int(seconds * 4 + frames * 150 + 800)
        credits += max(2, math.ceil(est_chars / 1000.0 * float(cfg.CREDIT_DEEPSEEK_PER_1K)) + 2)
    return credits


def estimate_cost(backend: str, recording: Optional[Recording],
                  payload: Dict[str, Any], cfg) -> Tuple[int, str, float]:
    """提交前预估：返回 (积分, 计量类型, 计量值)。"""
    if _task_kind(payload) == "transcribe":
        seconds = _media_seconds(recording, payload, cfg)
        credits = _transcribe_estimate(recording, payload, cfg, seconds)
        return max(1, credits), "media_seconds", seconds

    if backend == BACKEND_DEEPSEEK:
        options = (payload or {}).get("options") or {}
        prompt = options.get("prompt") or (payload or {}).get("text") or ""
        tokens = max(1, len(prompt or ""))  # 保守：中文约 1 字 ≈ 1 token
        credits = max(1, math.ceil(tokens / 1000.0 * float(cfg.CREDIT_DEEPSEEK_PER_1K)))
        return credits, "tokens", float(tokens)

    base, per_min = _gpu_pricing(backend, cfg)
    seconds = _media_seconds(recording, payload, cfg)
    credits = int(base) + int(math.ceil(seconds / 60.0 * per_min))
    return max(1, credits), "media_seconds", seconds


def actual_cost(backend: str, result: Dict[str, Any], recording: Optional[Recording],
                payload: Dict[str, Any], cfg) -> Tuple[int, str, float]:
    """完成后结算：DeepSeek 按 API 返回 token，GPU 按媒体时长；反馈#10 转写任务按组件结算。"""
    result = result or {}
    if _task_kind(payload) == "transcribe":
        # 转写部分：优先用 Whisper 返回的实际时长，缺失时回退录制时长/模态预估
        seconds = float(result.get("duration_sec") or 0) or _media_seconds(recording, payload, cfg)
        credits = int(cfg.CREDIT_TRANSCRIBE_BASE) + int(math.ceil(
            seconds / 60.0 * float(cfg.CREDIT_TRANSCRIBE_PER_MIN)))
        # 关键帧简述：按实际帧数
        keyframes = result.get("keyframes") or []
        if keyframes:
            credits += int(cfg.CREDIT_KEYFRAMES_BASE) + int(math.ceil(
                len(keyframes) * float(cfg.CREDIT_KEYFRAMES_PER_FRAME)))
        # 外部大模型详述：仅在成功调用（有 tokens_used 或 llm_summary）时计费
        tokens = float(result.get("tokens_used") or 0)
        if tokens > 0:
            credits += max(1, math.ceil(tokens / 1000.0 * float(cfg.CREDIT_DEEPSEEK_PER_1K)))
        elif result.get("llm_summary"):
            # API 未回 usage 时按结果长度保守计
            tokens = float(max(1, len(str(result.get("llm_summary") or ""))))
            credits += max(1, math.ceil(tokens / 1000.0 * float(cfg.CREDIT_DEEPSEEK_PER_1K)))
        return max(1, credits), "media_seconds", seconds

    if backend == BACKEND_DEEPSEEK:
        tokens = float(result.get("tokens_used") or 0)
        if tokens <= 0:
            options = (payload or {}).get("options") or {}
            tokens = float(max(1, len(options.get("prompt") or (payload or {}).get("text") or "")))
        credits = max(1, math.ceil(tokens / 1000.0 * float(cfg.CREDIT_DEEPSEEK_PER_1K)))
        return credits, "tokens", tokens

    base, per_min = _gpu_pricing(backend, cfg)
    seconds = _media_seconds(recording, payload, cfg)
    credits = int(base) + int(math.ceil(seconds / 60.0 * per_min))
    return max(1, credits), "media_seconds", seconds


# ---------------- 余额 ----------------

def enforcement_active(cfg) -> bool:
    """积分管控是否生效：总开关开启 且 管理员已发放过积分（未发放时不拦截，避免上线即锁死）。"""
    if not getattr(cfg, "CREDIT_ENABLED", True):
        return False
    try:
        return db.session.query(UserCreditGrant.id).first() is not None
    except Exception:
        return False


def rebuild_balance(user_id: int) -> int:
    """由 MySQL 流水重建余额：累计发放 - 成功消耗。"""
    granted = db.session.query(
        func.coalesce(func.sum(UserCreditGrant.credits), 0)
    ).filter(UserCreditGrant.user_id == user_id).scalar() or 0
    used = db.session.query(
        func.coalesce(func.sum(ComputeUsageLog.cost_credits), 0)
    ).filter(
        ComputeUsageLog.user_id == user_id,
        ComputeUsageLog.status == "success",
    ).scalar() or 0
    return int(granted) - int(used)


def get_balance(user_id: int, client: redis.Redis) -> int:
    key = balance_key(user_id)
    v = client.get(key)
    if v is None:
        bal = rebuild_balance(user_id)
        client.set(key, bal)
        return bal
    return int(v)


# ---------------- 冻结 / 结算 / 退还 ----------------

def freeze(user_id: int, est_cost: int, cfg, client: redis.Redis) -> str:
    """
    提交任务时冻结积分。返回 'held'（已冻结）或 'free'（不拦截）。
    余额不足抛 CreditInsufficient。
    """
    if not enforcement_active(cfg):
        return "free"
    u = db.session.get(User, user_id)
    if u and u.role == "super_admin":
        return "free"  # 超级管理员不受积分约束

    key = balance_key(user_id)
    try:
        if client.get(key) is None:
            client.set(key, rebuild_balance(user_id))
    except redis.RedisError:
        # 余额初始化失败不阻断：下面的 Lua 失败也会降级放行
        pass
    try:
        script = client.register_script(_FREEZE_LUA)
        rc = int(script(keys=[key], args=[int(est_cost)]))
    except redis.RedisError:
        # Redis 脚本异常时降级为不拦截（可用性优先；账单仍会在 MySQL 记录）
        return "free"
    if rc < 0:
        bal = get_balance(user_id, client)
        raise CreditInsufficient(
            f"积分余额不足：当前余额 {bal} 积分，本次任务约需 {est_cost} 积分，"
            f"可改用优化版(local)后端或联系管理员分配积分",
            balance=bal, need=int(est_cost),
        )
    return "held"


def refund(user_id: int, credits: int, client: redis.Redis) -> None:
    """退还冻结积分（任务失败 / 入队异常）。"""
    if credits and credits > 0:
        try:
            client.incrby(balance_key(user_id), int(credits))
        except redis.RedisError:
            pass


def _write_usage(task_id, user_id, backend, recording, payload, cost, est,
                 metric_type, metric_value, status, detail):
    modality = (payload or {}).get("modality") or (
        recording.modality if recording is not None and hasattr(recording, "modality") else None
    )
    row = ComputeUsageLog(
        user_id=user_id,
        backend=backend or "unknown",
        task_id=task_id,
        recording_id=(recording.id if recording is not None else (payload or {}).get("recording_id")),
        modality=modality,
        metric_type=metric_type,
        metric_value=float(metric_value or 0),
        cost_credits=int(cost or 0),
        est_credits=int(est or 0),
        status=status,
        detail=(detail or "")[:500],
    )
    db.session.add(row)


def _mark_billing(task_id: str, state: str, client: redis.Redis, task_key_prefix: str) -> None:
    try:
        client.hset(f"{task_key_prefix}{task_id}", mapping={"billing": state})
    except redis.RedisError:
        pass


def settle_task(task: Dict[str, Any], cfg, client: redis.Redis, task_key_prefix: str) -> Dict[str, Any]:
    """
    按任务 HASH 状态结算积分（幂等；在状态轮询/结果确认时由 Web 侧调用）。
    - done 且结果复用：记 reused 流水，免费；
    - done 正常：按实际后端/用量结算，冻结额多退少补；
    - failed/dead：冻结额全额退还，记 failed 流水。
    """
    tid = task.get("task_id")
    billing = task.get("billing") or ""
    status = task.get("status") or ""
    if billing in ("settled", "refunded"):
        return {"task_id": tid, "billing": billing, "noop": True}

    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    raw_uid = payload.get("submitted_by") or task.get("submitted_by")
    try:
        user_id = int(raw_uid) if raw_uid is not None else None
    except (TypeError, ValueError):
        user_id = None
    recording_id = payload.get("recording_id")
    recording = Recording.query.get(recording_id) if recording_id else None

    # 账单归属人缺失（HASH/消息均丢失 submitted_by）：无法记账与退款，
    # 直接标记结算完成避免轮询无限重试（流水不写，user_id 为 NOT NULL）
    if not user_id:
        _mark_billing(tid, "settled" if status == "done" else "refunded", client, task_key_prefix)
        return {"task_id": tid, "billing": billing, "noop": True, "reason": "no_submitted_by"}

    try:
        if status == "done":
            result = task.get("result") if isinstance(task.get("result"), dict) else {}

            # 1) MD5 结果复用：免积分
            if result.get("reused") or billing == "reused":
                _write_usage(tid, user_id, "cache", recording, payload, 0, 0,
                             "cache_hit", 1, "reused",
                             result.get("message") or "同文件(MD5)结果复用，免积分")
                db.session.commit()
                _mark_billing(tid, "settled", client, task_key_prefix)
                return {"task_id": tid, "billing": "settled", "cost": 0, "reused": True}

            # 2) 正常结算
            backend = result.get("backend") or ""
            if backend not in (BACKEND_AUTODL, BACKEND_LOCAL, BACKEND_DEEPSEEK):
                backend = select_backend(
                    payload, cfg.MODEL_DEFAULT_BACKEND,
                    getattr(cfg, "MODEL_DEFAULT_GPU_BACKEND", BACKEND_LOCAL),
                )
            cost, metric, value = actual_cost(backend, result, recording, payload, cfg)
            est = int(task.get("est_cost") or 0)
            # 仅“已冻结(held)”任务真正扣减积分；free（管控未生效/超管/Redis降级放行）
            # 任务流水 cost 记 0，避免 rebuild_balance 出现“未发放却有消耗”的负余额
            charged = cost if billing == "held" else 0
            if billing == "held" and user_id:
                diff = est - cost
                if diff != 0:
                    try:
                        client.incrby(balance_key(user_id), diff)  # 正数退还 / 负数补扣
                    except redis.RedisError:
                        pass
            detail = ""
            if result.get("failover_from"):
                detail = f"首选后端 {result['failover_from']} 不可用，已故障切换"
            if billing != "held":
                detail = (detail + "；" if detail else "") + "未冻结积分（管控未生效/免积分），本次计 0 积分"
            _write_usage(tid, user_id, backend, recording, payload, charged, est,
                         metric, value, "success", detail)
            db.session.commit()
            _mark_billing(tid, "settled", client, task_key_prefix)
            return {"task_id": tid, "billing": "settled", "cost": charged, "est": est,
                    "backend": backend, "metric": metric, "metric_value": value}

        if status in ("failed", "dead"):
            est = int(task.get("est_cost") or 0)
            if billing == "held" and est > 0 and user_id:
                refund(user_id, est, client)
            _write_usage(tid, user_id, payload.get("backend") or "unknown",
                         recording, payload, 0, est, "none", 0, "failed",
                         task.get("error") or "任务失败，冻结积分已退还")
            db.session.commit()
            _mark_billing(tid, "refunded", client, task_key_prefix)
            return {"task_id": tid, "billing": "refunded", "refund": est}

        return {"task_id": tid, "billing": billing, "noop": True}
    except Exception as e:
        db.session.rollback()
        # 不标记 billing，下次轮询会重试结算
        return {"task_id": tid, "billing": billing, "settle_error": str(e)}


# ---------------- 管理员发放 / 查询 ----------------

def grant_credits(admin_id: int, user_id: int, credits: int,
                  period: str, reason: str, client: redis.Redis) -> UserCreditGrant:
    """管理员发放积分：写持久记录并累加 Redis 余额。"""
    credits = int(credits)
    if credits <= 0:
        raise ValueError("发放积分必须为正整数")
    user = db.session.get(User, user_id)
    if not user:
        raise ValueError("用户不存在")
    row = UserCreditGrant(
        user_id=user_id,
        credits=credits,
        period=(period or "permanent")[:16],
        reason=(reason or "")[:250],
        granted_by=admin_id,
    )
    db.session.add(row)
    db.session.commit()
    try:
        if client.get(balance_key(user_id)) is None:
            client.set(balance_key(user_id), rebuild_balance(user_id))
        else:
            client.incrby(balance_key(user_id), credits)
    except redis.RedisError:
        pass
    return row


def admin_overview(client: redis.Redis):
    """管理员视角：全体用户余额 / 累计发放 / 累计消耗。"""
    grants = dict(db.session.query(
        UserCreditGrant.user_id, func.coalesce(func.sum(UserCreditGrant.credits), 0)
    ).group_by(UserCreditGrant.user_id).all())
    used = dict(db.session.query(
        ComputeUsageLog.user_id, func.coalesce(func.sum(ComputeUsageLog.cost_credits), 0)
    ).filter(ComputeUsageLog.status == "success").group_by(ComputeUsageLog.user_id).all())

    users = User.query.order_by(User.id.asc()).all()
    rows = []
    for u in users:
        rows.append({
            "user_id": u.id,
            "username": u.username,
            "nickname": getattr(u, "nickname", None) or "",
            "full_name": u.full_name or "",
            "role": u.role,
            "is_active": bool(u.is_active),
            "balance": get_balance(u.id, client),
            "granted_total": int(grants.get(u.id, 0)),
            "used_total": int(used.get(u.id, 0)),
        })
    return rows


def my_credits(user_id: int, cfg, client: redis.Redis, limit: int = 10):
    """普通用户：我的余额 + 近期算力流水。"""
    rows = (
        ComputeUsageLog.query.filter_by(user_id=user_id)
        .order_by(ComputeUsageLog.created_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "enabled": enforcement_active(cfg),
        "balance": get_balance(user_id, client),
        "pricing": {
            "deepseek_per_1k_tokens": cfg.CREDIT_DEEPSEEK_PER_1K,
            "autodl_base": cfg.CREDIT_AUTODL_BASE,
            "autodl_per_min": cfg.CREDIT_AUTODL_PER_MIN,
            "local_base": cfg.CREDIT_LOCAL_BASE,
            "local_per_min": cfg.CREDIT_LOCAL_PER_MIN,
        },
        "recent": [
            {
                "backend": r.backend,
                "recording_id": r.recording_id,
                "modality": r.modality,
                "metric_type": r.metric_type,
                "metric_value": r.metric_value,
                "cost_credits": r.cost_credits,
                "status": r.status,
                "detail": r.detail,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }
