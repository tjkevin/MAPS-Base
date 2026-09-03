import json
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import redis


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_value(v: Any) -> str:
    """HASH 字段统一序列化为字符串（dict/list 走 JSON，其余 str()）。"""
    if isinstance(v, str):
        return v
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


# 原子搬运：ZREM 成功才 RPUSH（避免先 zrem 后 rpush 失败丢消息）
_MOVE_LUA = """
local moved = redis.call('ZREM', KEYS[1], ARGV[1])
if moved == 1 then
    redis.call('RPUSH', KEYS[2], ARGV[1])
    return 1
end
return 0
"""

# CAS 回写：仅当 HASH 当前 status（及 worker_id）匹配时才写入，
# 防止 worker 迟到结果覆盖恢复扫描已判定失败/已退款的任务
_CAS_UPDATE_LUA = """
local key = KEYS[1]
local cur_status = redis.call('HGET', key, 'status')
if cur_status ~= ARGV[1] then
    return 0
end
if ARGV[2] ~= '' then
    local cur_worker = redis.call('HGET', key, 'worker_id')
    if cur_worker ~= ARGV[2] then
        return 0
    end
end
for i = 3, #ARGV - 1, 2 do
    redis.call('HSET', key, ARGV[i], ARGV[i+1])
end
redis.call('EXPIRE', key, tonumber(ARGV[#ARGV]))
return 1
"""


class BagelQueueService:
    """
    Redis-backed queue + cache for asynchronous model tasks.
    - Queue:            Redis LIST  (LPUSH 生产 / BRPOP 竞争消费)
    - Delayed retry:    Redis ZSET  (score = 下次可执行时间戳，到期由 worker 搬回队列)
    - Dead letter:      Redis LIST  (超过最大重试次数的任务，供管理员查看/重投)
    - Worker heartbeat: Redis STRING (bagel:worker:<id>，TTL 过期即视为下线)
    - Task state:       Redis HASH  (status/payload/result/error/billing...)
    """

    def __init__(
        self,
        redis_url: str,
        queue_name: str = "bagel:task:queue",
        task_key_prefix: str = "bagel:task:",
        result_ttl_seconds: int = 86400,
        delayed_zset: str = "bagel:task:delayed",
        dead_queue_name: str = "bagel:task:dead",
        worker_prefix: str = "bagel:worker:",
        dead_ttl_seconds: int = 7 * 86400,
    ):
        self.redis_url = redis_url
        self.queue_name = queue_name
        self.task_key_prefix = task_key_prefix
        self.result_ttl_seconds = result_ttl_seconds
        self.dead_ttl_seconds = dead_ttl_seconds
        self.delayed_zset = delayed_zset
        self.dead_queue_name = dead_queue_name
        self.worker_prefix = worker_prefix
        self.client = redis.Redis.from_url(redis_url, decode_responses=True)
        self._move_script = self.client.register_script(_MOVE_LUA)
        self._cas_update_script = self.client.register_script(_CAS_UPDATE_LUA)

    def health(self) -> bool:
        try:
            return bool(self.client.ping())
        except redis.RedisError:
            return False

    def _task_key(self, task_id: str) -> str:
        return f"{self.task_key_prefix}{task_id}"

    # ---------------- 入队 / 查询 / 更新 ----------------

    def enqueue(self, payload: Dict[str, Any], extra: Optional[Dict[str, Any]] = None) -> str:
        task_id = str(uuid.uuid4())
        now = _utc_now_iso()
        task_key = self._task_key(task_id)

        # task cache
        task_state = {
            "task_id": task_id,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "payload": json.dumps(payload, ensure_ascii=False),
            "result": "",
            "error": "",
            "attempts": "0",
        }
        if extra:
            for k, v in extra.items():
                task_state[k] = _hash_value(v)
        self.client.hset(task_key, mapping=task_state)
        self.client.expire(task_key, self.result_ttl_seconds)

        # queue message（同时携带账单上下文字段：LIST 无 TTL，
        # HASH 过期后仍可凭消息重建，避免结算时丢失 submitted_by/est_cost/billing）
        queue_msg = {"task_id": task_id, "payload": payload, "created_at": now}
        if extra:
            for k in ("billing", "est_cost", "submitted_by", "backend_selected"):
                if k in extra:
                    queue_msg[k] = extra[k]
        self.client.lpush(self.queue_name, json.dumps(queue_msg, ensure_ascii=False))
        return task_id

    def enqueue_reused(self, payload: Dict[str, Any], result: Dict[str, Any]) -> str:
        """MD5 命中复用：直接构造一个已完成的任务（不消耗算力、不进队列）。"""
        task_id = str(uuid.uuid4())
        now = _utc_now_iso()
        task_key = self._task_key(task_id)
        task_state = {
            "task_id": task_id,
            "status": "done",
            "created_at": now,
            "updated_at": now,
            "payload": json.dumps(payload, ensure_ascii=False),
            "result": json.dumps(result, ensure_ascii=False),
            "error": "",
            "attempts": "0",
            "billing": "reused",
        }
        self.client.hset(task_key, mapping=task_state)
        self.client.expire(task_key, self.result_ttl_seconds)
        return task_id

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        task_key = self._task_key(task_id)
        data = self.client.hgetall(task_key)
        if not data:
            return None
        parsed = dict(data)
        if parsed.get("payload"):
            try:
                parsed["payload"] = json.loads(parsed["payload"])
            except json.JSONDecodeError:
                pass
        if parsed.get("result"):
            try:
                parsed["result"] = json.loads(parsed["result"])
            except json.JSONDecodeError:
                pass
        return parsed

    def update_task(self, task_id: str, status: str, result: Any = None, error: str = "",
                    extra: Optional[Dict[str, Any]] = None) -> None:
        task_key = self._task_key(task_id)
        mapping = {"status": status, "updated_at": _utc_now_iso(), "error": error or ""}
        if result is not None:
            mapping["result"] = json.dumps(result, ensure_ascii=False)
        if extra:
            for k, v in extra.items():
                mapping[k] = _hash_value(v)
        self.client.hset(task_key, mapping=mapping)
        self.client.expire(task_key, self.result_ttl_seconds)

    def update_task_cas(self, task_id: str, expected_status: str, fields: Dict[str, Any],
                        expected_worker: str = "") -> bool:
        """
        CAS 条件回写：仅当 HASH 当前 status == expected_status（且 worker_id 匹配）时才写入。
        用于 worker 回写 done/failed：若任务已被恢复扫描判失败并退款，迟到结果不再覆盖。
        返回是否写入成功。
        """
        fields = dict(fields or {})
        fields.setdefault("updated_at", _utc_now_iso())
        args: List[Any] = [expected_status, expected_worker or ""]
        for k, v in fields.items():
            args.extend([k, _hash_value(v)])
        args.append(str(self.result_ttl_seconds))
        try:
            rc = self._cas_update_script(keys=[self._task_key(task_id)], args=args)
            return bool(rc)
        except redis.RedisError:
            # Redis 异常时退化为普通回写（可用性优先）
            try:
                mapping = {k: _hash_value(v) for k, v in fields.items()}
                mapping.setdefault("updated_at", _utc_now_iso())
                self.client.hset(self._task_key(task_id), mapping=mapping)
                self.client.expire(self._task_key(task_id), self.result_ttl_seconds)
                return True
            except redis.RedisError:
                return False

    def ensure_task_fields(self, task_id: str, msg: Dict[str, Any]) -> None:
        """
        HASH 过期/字段缺失时，从队列消息重建关键字段
        （payload/billing/est_cost/submitted_by/backend_selected），保证结算上下文不丢。
        """
        task_key = self._task_key(task_id)
        if not self.client.exists(task_key):
            self.client.hset(task_key, mapping={
                "task_id": task_id,
                "created_at": msg.get("created_at") or _utc_now_iso(),
                "updated_at": _utc_now_iso(),
                "status": "queued",
                "result": "",
                "error": "",
                "attempts": str(msg.get("attempts", 0)),
            })
        mapping: Dict[str, str] = {}
        payload = msg.get("payload")
        if payload is not None and not self.client.hexists(task_key, "payload"):
            mapping["payload"] = json.dumps(payload, ensure_ascii=False)
        for f in ("billing", "est_cost", "submitted_by", "backend_selected"):
            if msg.get(f) is not None and not self.client.hexists(task_key, f):
                mapping[f] = str(msg.get(f))
        if mapping:
            self.client.hset(task_key, mapping=mapping)
        self.client.expire(task_key, self.result_ttl_seconds)

    def worker_alive(self, worker_id: str) -> bool:
        """worker 心跳键是否存在（Redis 异常时返回 True，避免误判杀任务）。"""
        if not worker_id:
            return False
        try:
            return bool(self.client.exists(f"{self.worker_prefix}{worker_id}"))
        except redis.RedisError:
            return True

    # ---------------- Worker 心跳 ----------------

    def heartbeat(self, worker_id: str, info: Dict[str, Any], ttl_seconds: int = 90) -> None:
        info = dict(info or {})
        info["seen_at"] = _utc_now_iso()
        try:
            self.client.set(f"{self.worker_prefix}{worker_id}", json.dumps(info, ensure_ascii=False), ex=ttl_seconds)
        except redis.RedisError:
            pass

    def list_workers(self) -> List[Dict[str, Any]]:
        out = []
        try:
            keys = list(self.client.scan_iter(match=f"{self.worker_prefix}*", count=100))
            for k in keys:
                raw = self.client.get(k)
                if not raw:
                    continue
                try:
                    info = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                info["worker_id"] = k[len(self.worker_prefix):]
                out.append(info)
        except redis.RedisError:
            pass
        out.sort(key=lambda x: x.get("seen_at", ""), reverse=True)
        return out

    # ---------------- 延迟重试 / 死信 ----------------

    def promote_due_delayed(self) -> int:
        """把到期的延迟重试任务搬回主队列。返回搬运数量。"""
        now = time.time()
        try:
            due = self.client.zrangebyscore(self.delayed_zset, 0, now)
        except redis.RedisError:
            return 0
        moved = 0
        for raw in due:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                self.client.zrem(self.delayed_zset, raw)
                continue
            try:
                # Lua 原子搬运：ZREM 成功才 RPUSH，不会出现“删了没投”的丢消息窗口
                rc = self._move_script(keys=[self.delayed_zset, self.queue_name], args=[raw])
            except redis.RedisError as e:
                # Redis 异常：成员仍在 ZSET，下轮继续（不吞后续任务，但先退出本轮）
                print(f"promote_due_delayed move failed: {e}", file=sys.stderr)
                break
            if rc:
                tid = msg.get("task_id", "")
                try:
                    self.ensure_task_fields(tid, msg)
                    self.update_task(tid, status="queued",
                                     error=msg.get("last_error", ""),
                                     extra={"attempts": msg.get("attempts", 0)})
                except redis.RedisError as e:
                    print(f"promote_due_delayed state update failed for {tid}: {e}", file=sys.stderr)
                moved += 1
        return moved

    def requeue_later(self, msg: Dict[str, Any], attempts: int, delay_seconds: int,
                      error: str = "") -> None:
        """失败任务进入延迟重试：ZSET score = 现在 + 退避秒数。"""
        msg = dict(msg)
        msg["attempts"] = attempts
        msg["last_error"] = (error or "")[:500]
        run_at = time.time() + max(1, delay_seconds)
        self.client.zadd(self.delayed_zset, {json.dumps(msg, ensure_ascii=False): run_at})
        self.update_task(msg.get("task_id", ""), status="retry_wait",
                         error=(error or "")[:500], extra={"attempts": attempts, "next_run_at": run_at})

    def dead_letter(self, msg: Dict[str, Any], attempts: int, error: str = "",
                    expected_worker: str = "") -> None:
        """
        超过最大重试次数/不可重试错误：进入死信队列，任务状态标记 dead。
        expected_worker 非空时以 CAS 回写状态（仅 status=running 且 worker_id 匹配才置 dead），
        避免迟到 worker 覆盖恢复扫描已判定失败/已退款的任务。
        """
        record = {
            "msg": msg,
            "attempts": attempts,
            "error": (error or "")[:1000],
            "dead_at": _utc_now_iso(),
        }
        self.client.lpush(self.dead_queue_name, json.dumps(record, ensure_ascii=False))
        # 死信队列只保留最近 200 条，防止无限增长
        self.client.ltrim(self.dead_queue_name, 0, 199)
        tid = msg.get("task_id", "") if isinstance(msg, dict) else ""
        if tid and expected_worker:
            # CAS：任务已被恢复扫描判失败（status != running 或 worker 已易主）则不覆盖状态
            self.update_task_cas(
                tid, "running",
                {"status": "dead", "error": (error or "")[:1000], "attempts": attempts},
                expected_worker=expected_worker,
            )
        else:
            self.update_task(tid, status="dead", error=(error or "")[:1000],
                             extra={"attempts": attempts})
        # 死信可能长期留存等待管理员处理：HASH TTL 延长到 7 天，且重投时可凭死信消息重建
        try:
            self.client.expire(self._task_key(tid), self.dead_ttl_seconds)
        except redis.RedisError:
            pass

    def list_dead(self, limit: int = 50) -> List[Dict[str, Any]]:
        out = []
        try:
            raws = self.client.lrange(self.dead_queue_name, 0, limit - 1)
        except redis.RedisError:
            return out
        for idx, raw in enumerate(raws):
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            rec["dead_index"] = idx
            out.append(rec)
        return out

    def requeue_dead(self, task_id: str) -> bool:
        """管理员把死信任务重新投回主队列。"""
        raws = self.client.lrange(self.dead_queue_name, 0, -1)
        for raw in raws:
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg = rec.get("msg") or {}
            if msg.get("task_id") != task_id:
                continue
            msg["attempts"] = 0
            msg.pop("last_error", None)
            self.client.lrem(self.dead_queue_name, 1, raw)
            self.client.rpush(self.queue_name, json.dumps(msg, ensure_ascii=False))
            # HASH 可能已随 24h TTL 过期：凭死信消息重建 payload/账单上下文后再改状态
            self.ensure_task_fields(task_id, msg)
            self.update_task(task_id, status="queued", error="", extra={"attempts": 0})
            return True
        return False

    # ---------------- 卡死任务扫描（管理员触发恢复）----------------

    def scan_stuck(self, queued_timeout: int, running_timeout: int) -> Dict[str, List[str]]:
        """
        扫描任务 HASH：
        - queued 超过 queued_timeout 秒仍未被消费（队列消息丢失/worker 全下线）→ 可重投
        - running 超过 running_timeout 秒且对应 worker 心跳已消失（worker 崩溃未回写）→ 可判失败；
          心跳仍在说明是长推理（下载/推理可达数十分钟），不误判
        - retry_wait 由延迟 ZSET 到期自动搬运，不在此扫描（原死分支已移除）
        返回 {'stuck_queued': [task_id...], 'stuck_running': [task_id...]}。
        """
        now_ts = time.time()
        stuck_queued, stuck_running = [], []
        try:
            key_iter = self.client.scan_iter(match=f"{self.task_key_prefix}*", count=200)
            keys = list(key_iter)
        except redis.RedisError:
            return {"stuck_queued": stuck_queued, "stuck_running": stuck_running}
        for k in keys:
            try:
                data = self.client.hgetall(k)
            except redis.RedisError:
                continue
            if not data:
                continue
            status = data.get("status")
            if status not in ("queued", "running"):
                continue
            ts = data.get("updated_at") or data.get("created_at")
            try:
                age = now_ts - datetime.fromisoformat(ts).timestamp()
            except (TypeError, ValueError):
                continue
            tid = data.get("task_id") or k[len(self.task_key_prefix):]
            if status == "queued" and age > queued_timeout:
                stuck_queued.append(tid)
            elif status == "running" and age > running_timeout:
                # 心跳仍在（dispatch 期间续租线程持续刷新）→ 长推理，不算卡死
                if self.worker_alive(data.get("worker_id") or ""):
                    continue
                stuck_running.append(tid)
        return {"stuck_queued": stuck_queued, "stuck_running": stuck_running}

    def requeue_task_by_id(self, task_id: str) -> bool:
        """按 task_id 从 HASH 重建队列消息并重投（用于卡死恢复）。"""
        task = self.get_task(task_id)
        if not task:
            return False
        payload = task.get("payload") if isinstance(task.get("payload"), dict) else None
        if not payload:
            return False
        msg = {"task_id": task_id, "payload": payload, "created_at": _utc_now_iso(), "requeued": True}
        # 账单上下文随消息带走，防止重投后 HASH 过期丢字段
        for f in ("billing", "est_cost", "submitted_by", "backend_selected"):
            if task.get(f) is not None:
                msg[f] = task.get(f)
        self.ensure_task_fields(task_id, msg)
        self.client.rpush(self.queue_name, json.dumps(msg, ensure_ascii=False))
        # 清掉旧 worker_id：新 worker 领取后 CAS 以新 worker 身份回写
        self.update_task(task_id, status="queued", error="",
                         extra={"attempts": 0, "worker_id": ""})
        return True

    # ---------------- 实时监控（反馈#8：队列在跑数/进度/所用模型）----------------

    def live_overview(self, limit_per_status: int = 100) -> Dict[str, Any]:
        """
        扫描全部任务 HASH 聚合：
        - counts: 各状态（queued/running/done/failed/dead/retry_wait）数量
        - running: 进行中任务明细（worker/后端/已耗时/尝试次数/提交人/媒体信息）
        - queued:  排队中任务明细（按创建时间升序，近似 FIFO）
        - retry_wait: 延迟重试等待中任务明细（含 next_run_at 时间戳）
        """
        counts = {"queued": 0, "running": 0, "done": 0,
                  "failed": 0, "dead": 0, "retry_wait": 0}
        running: List[Dict[str, Any]] = []
        queued: List[Dict[str, Any]] = []
        retry_wait: List[Dict[str, Any]] = []
        try:
            keys = list(self.client.scan_iter(match=f"{self.task_key_prefix}*", count=300))
        except redis.RedisError:
            return {"counts": counts, "running": running, "queued": queued, "retry_wait": retry_wait}
        for k in keys:
            try:
                data = self.client.hgetall(k)
            except redis.RedisError:
                continue
            if not data:
                continue
            st = data.get("status") or "queued"
            counts[st] = counts.get(st, 0) + 1
            if st not in ("running", "queued", "retry_wait"):
                continue
            tid = data.get("task_id") or k[len(self.task_key_prefix):]
            payload = {}
            if data.get("payload"):
                try:
                    payload = json.loads(data["payload"])
                except json.JSONDecodeError:
                    payload = {}
            opts = payload.get("options") if isinstance(payload.get("options"), dict) else {}
            item = {
                "task_id": tid,
                "status": st,
                "backend_selected": data.get("backend_selected", ""),
                "worker_id": data.get("worker_id", ""),
                "attempts": int(data.get("attempts") or 0),
                "est_cost": data.get("est_cost", ""),
                "billing": data.get("billing", ""),
                "submitted_by": data.get("submitted_by", "") or str(payload.get("submitted_by", "")),
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at", ""),
                "error": (data.get("error") or "")[:200],
                "recording_id": payload.get("recording_id", ""),
                "filename": payload.get("filename", ""),
                "modality": payload.get("modality", ""),
                "task_kind": str(opts.get("task_kind") or "understand"),
                # 反馈#10：Whisper 转写任务无 prompt，监控页展示组件组合
                "prompt_hint": (
                    "Whisper转写"
                    + ("+关键帧简述" if opts.get("with_keyframes") else "")
                    + ("+外部大模型详述" if opts.get("with_llm_summary") else "")
                ) if str(opts.get("task_kind") or "") == "transcribe"
                else (opts.get("prompt") or opts.get("system_prompt") or "")[:80],
            }
            if st == "running":
                running.append(item)
            elif st == "queued":
                queued.append(item)
            else:
                item["next_run_at"] = data.get("next_run_at", "")
                retry_wait.append(item)
        running.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        queued.sort(key=lambda x: x.get("created_at", ""))
        retry_wait.sort(key=lambda x: x.get("next_run_at", ""))
        return {
            "counts": counts,
            "running": running[:limit_per_status],
            "queued": queued[:limit_per_status],
            "retry_wait": retry_wait[:limit_per_status],
        }
