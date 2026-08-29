import json
import os
import time
from typing import Any, Dict
import sys

import requests
import redis

# Ensure project root is importable when running from scripts/
_script_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_script_dir)
if _root not in sys.path:
    sys.path.insert(0, _root)
os.chdir(_root)

from config import Config
from services.bagel_queue import BagelQueueService


def process_with_bagel(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Call BAGEL model service.
    Expected BAGEL API:
      POST {BAGEL_SERVICE_URL}/infer
      body: {"file_path": "...", "options": {...}}
    """
    bagel_url = payload.get("bagel_service_url") or Config.BAGEL_SERVICE_URL
    endpoint = f"{bagel_url.rstrip('/')}/infer"
    body = {"file_path": payload.get("file_path"), "options": payload.get("options", {})}
    resp = requests.post(endpoint, json=body, timeout=1200)
    resp.raise_for_status()
    return resp.json()


def main():
    queue = BagelQueueService(
        redis_url=Config.REDIS_URL,
        queue_name=Config.BAGEL_QUEUE_NAME,
        task_key_prefix=Config.BAGEL_TASK_KEY_PREFIX,
        result_ttl_seconds=Config.BAGEL_RESULT_TTL_SECONDS,
    )
    client = redis.Redis.from_url(Config.REDIS_URL, decode_responses=True)

    print("BAGEL worker started.")
    print(f"Queue: {Config.BAGEL_QUEUE_NAME}")
    print(f"BAGEL service: {Config.BAGEL_SERVICE_URL}")

    while True:
        try:
            item = client.brpop(Config.BAGEL_QUEUE_NAME, timeout=5)
            if not item:
                continue
            _, raw_msg = item
            msg = json.loads(raw_msg)
            task_id = msg["task_id"]
            payload = msg["payload"]
            queue.update_task(task_id, status="running")
            try:
                result = process_with_bagel(payload)
                queue.update_task(task_id, status="done", result=result)
            except Exception as e:
                queue.update_task(task_id, status="failed", error=str(e))
        except KeyboardInterrupt:
            print("Worker stopped by user.")
            break
        except Exception as e:
            print(f"Worker loop error: {e}")
            time.sleep(2)


if __name__ == "__main__":
    main()

