import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import redis


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BagelQueueService:
    """
    Redis-backed queue + cache for asynchronous BAGEL tasks.
    Queue: Redis LIST
    Task status/result cache: Redis HASH
    """

    def __init__(
        self,
        redis_url: str,
        queue_name: str = "bagel:task:queue",
        task_key_prefix: str = "bagel:task:",
        result_ttl_seconds: int = 86400,
    ):
        self.redis_url = redis_url
        self.queue_name = queue_name
        self.task_key_prefix = task_key_prefix
        self.result_ttl_seconds = result_ttl_seconds
        self.client = redis.Redis.from_url(redis_url, decode_responses=True)

    def health(self) -> bool:
        try:
            return bool(self.client.ping())
        except redis.RedisError:
            return False

    def _task_key(self, task_id: str) -> str:
        return f"{self.task_key_prefix}{task_id}"

    def enqueue(self, payload: Dict[str, Any]) -> str:
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
        }
        self.client.hset(task_key, mapping=task_state)
        self.client.expire(task_key, self.result_ttl_seconds)

        # queue message
        queue_msg = {"task_id": task_id, "payload": payload, "created_at": now}
        self.client.lpush(self.queue_name, json.dumps(queue_msg, ensure_ascii=False))
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

    def update_task(self, task_id: str, status: str, result: Any = None, error: str = "") -> None:
        task_key = self._task_key(task_id)
        mapping = {"status": status, "updated_at": _utc_now_iso(), "error": error or ""}
        if result is not None:
            mapping["result"] = json.dumps(result, ensure_ascii=False)
        self.client.hset(task_key, mapping=mapping)
        self.client.expire(task_key, self.result_ttl_seconds)

