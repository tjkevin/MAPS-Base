"""
MAPS 模型任务 Worker
====================
同一份代码可部署在三个位置（云端不跑模型，仅做调度；模型在外部 GPU 服务运行）：

1. 云端容器（docker-compose profile=worker）：
   - bagel 后端：文件直接读 /app/uploads，multipart 推送给 autoDL/局域网推理服务
   - deepseek 后端：直接调用 DeepSeek API（受配额约束）
2. autoDL 租用卡实例：运行本 worker，从云端 Redis 拉任务，
   凭 file_download_url 下载文件后调用本机 BAGEL 推理服务（localhost）
3. 局域网 GPU 机器：同 autoDL，主动出栈连接云端 Redis，无需公网 IP / 内网穿透

可靠性机制：
- 心跳：每轮循环写 bagel:worker:<id>（TTL），dispatch 长任务期间由后台线程续租，
  防止下载(600s)/推理(1800s) 期间心跳过期被误判为 worker 崩溃
- 重试：任务失败按指数退避进入延迟队列（ZSET），最多 BAGEL_MAX_ATTEMPTS 次；
  配置缺失/HTTP 4xx 等不可重试错误直接入死信，不浪费重试次数
- 死信：超过重试次数/不可重试错误进入 bagel:task:dead，管理员可在 Web 端一键重投
- CAS 回写：done/failed/dead 仅当任务仍为本 worker 占有的 running 态才写入，
  迟到结果不会覆盖恢复扫描已判定失败并退款的任务
- 故障切换：首选 GPU 端点不健康时自动切到另一个健康端点（见 model_backends.dispatch）

外部部署示例（autoDL / 局域网机器）：
    pip install -r docs/requirements.txt        # 仅需 redis + requests
    export REDIS_URL=redis://:<密码>@<云端IP>:6379/0
    export BAGEL_SERVICE_URL=http://127.0.0.1:8000
    export MODEL_FILE_BASE_URL=https://maps.your-domain.com
    export MODEL_FILE_TOKEN=<与云端一致的共享密钥>
    python scripts/bagel_worker.py
"""
import json
import os
import socket
import sys
import threading
import time
from typing import Any, Callable, Dict

import redis

# Ensure project root is importable when running from scripts/
_script_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_script_dir)
if _root not in sys.path:
    sys.path.insert(0, _root)
os.chdir(_root)

from config import Config
from services.bagel_queue import BagelQueueService, _utc_now_iso
from services.model_backends import (
    FatalBackendError,
    QuotaExceeded,
    build_file_download_url,
    dispatch,
)


WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"


class HeartbeatRenewer(threading.Thread):
    """
    dispatch 长任务期间后台续租心跳（daemon 线程）。
    循环顶部心跳只能覆盖 brpop 阻塞（30s），下载+推理可达数十分钟，
    无续租会导致心跳键（TTL 90s）过期，被恢复扫描误判为 worker 崩溃。
    """

    def __init__(self, queue: BagelQueueService, worker_id: str,
                 ttl_seconds: int, interval_seconds: int, info_fn: Callable[[], Dict[str, Any]]):
        super().__init__(daemon=True)
        self.queue = queue
        self.worker_id = worker_id
        self.ttl_seconds = ttl_seconds
        self.interval_seconds = interval_seconds
        self.info_fn = info_fn
        self._stop_event = threading.Event()

    def run(self):
        while not self._stop_event.wait(self.interval_seconds):
            try:
                self.queue.heartbeat(self.worker_id, self.info_fn(), ttl_seconds=self.ttl_seconds)
            except Exception as e:
                print(f"heartbeat renew error: {e}", file=sys.stderr)

    def stop(self):
        self._stop_event.set()


def main():
    queue = BagelQueueService(
        redis_url=Config.REDIS_URL,
        queue_name=Config.BAGEL_QUEUE_NAME,
        task_key_prefix=Config.BAGEL_TASK_KEY_PREFIX,
        result_ttl_seconds=Config.BAGEL_RESULT_TTL_SECONDS,
        delayed_zset=getattr(Config, "BAGEL_DELAYED_ZSET", "bagel:task:delayed"),
        dead_queue_name=getattr(Config, "BAGEL_DEAD_QUEUE_NAME", "bagel:task:dead"),
        worker_prefix=getattr(Config, "BAGEL_WORKER_PREFIX", "bagel:worker:"),
    )
    # socket_timeout 必须大于 brpop 的阻塞 timeout：
    # redis-py 阻塞命令会临时把 socket 超时设为命令 timeout，若二者相等(如5s)，
    # 服务端 nil 回复(约5~5.6s)到达前客户端 socket 先超时，报 "Timeout reading from socket"
    client = redis.Redis.from_url(
        Config.REDIS_URL,
        decode_responses=True,
        socket_timeout=60,
        socket_connect_timeout=10,
    )

    max_attempts = max(1, int(getattr(Config, "BAGEL_MAX_ATTEMPTS", 3)))
    backoff_base = int(getattr(Config, "BAGEL_RETRY_BACKOFF_SECONDS", 60))
    heartbeat_ttl = int(getattr(Config, "BAGEL_WORKER_TTL_SECONDS", 90))

    print("MAPS model worker started.")
    print(f"Worker ID: {WORKER_ID}")
    print(f"Queue:   {Config.BAGEL_QUEUE_NAME}")
    print(f"Retry:   max_attempts={max_attempts}, backoff_base={backoff_base}s")
    print(f"Default backend: {Config.MODEL_DEFAULT_BACKEND} "
          f"(GPU default: {getattr(Config, 'MODEL_DEFAULT_GPU_BACKEND', 'local')})")
    print(f"  autodl (BAGEL满血版): {getattr(Config, 'BAGEL_AUTODL_SERVICE_URL', '') or Config.BAGEL_SERVICE_URL}")
    print(f"  local  (BAGEL优化版): {getattr(Config, 'BAGEL_LOCAL_SERVICE_URL', '') or Config.BAGEL_SERVICE_URL}")
    print(f"  file transfer: {Config.BAGEL_FILE_TRANSFER}")
    print(f"DeepSeek: {'configured' if Config.DEEPSEEK_API_KEY else 'not configured'} "
          f"(daily limit={Config.DEEPSEEK_DAILY_TOKEN_LIMIT}, monthly limit={Config.DEEPSEEK_MONTHLY_TOKEN_LIMIT})")

    dead_queue_name = getattr(Config, "BAGEL_DEAD_QUEUE_NAME", "bagel:task:dead")
    heartbeat_interval = max(10, heartbeat_ttl // 3)

    def _hb_info(task_id: str) -> Dict[str, Any]:
        return {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "current_task": task_id,
            "default_backend": Config.MODEL_DEFAULT_BACKEND,
        }

    current_task_id = ""
    while True:
        try:
            # 1) 心跳（每轮刷新，brpop 最多阻塞 30s，TTL 90s 足够覆盖；
            #    dispatch 长任务期间由 HeartbeatRenewer 后台线程按 TTL/3 间隔续租）
            queue.heartbeat(WORKER_ID, _hb_info(current_task_id), ttl_seconds=heartbeat_ttl)

            # 2) 到期的延迟重试任务搬回主队列
            moved = queue.promote_due_delayed()
            if moved:
                print(f"Promoted {moved} delayed task(s) back to queue")

            # 3) 阻塞取任务
            item = client.brpop(Config.BAGEL_QUEUE_NAME, timeout=30)
            if not item:
                current_task_id = ""
                continue
            _, raw_msg = item
            try:
                msg = json.loads(raw_msg)
                task_id = msg["task_id"]
                payload = msg["payload"]
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                # 毒消息：无法解析或缺 task_id/payload，重投会无限循环，
                # 记入死信队列留痕（管理员可见）后直接丢弃，不阻断消费循环
                print(f"Poison message discarded: {type(e).__name__}: {e}", file=sys.stderr)
                try:
                    client.lpush(dead_queue_name, json.dumps({
                        "msg": None,
                        "attempts": 0,
                        "error": f"毒消息(无法解析，已丢弃): {type(e).__name__}: {str(e)[:200]}",
                        "raw": (raw_msg or "")[:500],
                        "dead_at": _utc_now_iso(),
                    }, ensure_ascii=False))
                    client.ltrim(dead_queue_name, 0, 199)
                except redis.RedisError:
                    pass
                continue
            attempts = int(msg.get("attempts", 0)) + 1
            current_task_id = task_id

            # 外部 worker（autoDL/局域网）需要凭 URL 下载云端文件
            if not payload.get("file_download_url"):
                url = build_file_download_url(payload, Config)
                if url:
                    payload["file_download_url"] = url

            # 领取任务：置 running 并登记 worker_id（CAS 回写时核对身份）
            queue.update_task(task_id, status="running",
                              extra={"attempts": attempts, "worker_id": WORKER_ID})
            # dispatch 期间（下载最长 600s + 推理最长 1800s）后台续租心跳
            renewer = HeartbeatRenewer(
                queue, WORKER_ID, heartbeat_ttl, heartbeat_interval,
                lambda: _hb_info(task_id),
            )
            renewer.start()
            try:
                backend, result = dispatch(payload, Config, client, Config.UPLOAD_FOLDER)
                if isinstance(result, dict):
                    result.setdefault("backend", backend)
                print(f"[{task_id}] attempt={attempts} backend={backend} done")
                ok = queue.update_task_cas(
                    task_id, "running",
                    {"status": "done", "result": result, "attempts": attempts},
                    expected_worker=WORKER_ID,
                )
                if not ok:
                    # 任务已被恢复扫描判失败并退款：迟到结果不再覆盖，避免“退款与结果兼得”
                    print(f"[{task_id}] done but CAS lost (already recovered/refunded); "
                          f"result discarded", file=sys.stderr)
            except QuotaExceeded as e:
                # 配额/积分不足：不重试（重试也不会成功），直接失败
                print(f"[{task_id}] quota exceeded: {e}")
                queue.update_task_cas(
                    task_id, "running",
                    {"status": "failed", "error": f"配额限制: {e}", "attempts": attempts},
                    expected_worker=WORKER_ID,
                )
            except FatalBackendError as e:
                # 配置缺失 / HTTP 4xx / payload 非法等不可重试错误：直接入死信，
                # 管理员排障（改配置/修数据）后可在 Web 端一键重投
                err = f"{type(e).__name__}: {e}"
                print(f"[{task_id}] attempt={attempts} fatal (non-retryable): {err} -> dead letter")
                queue.dead_letter(msg, attempts, error=f"永久失败(不重试): {err}",
                                  expected_worker=WORKER_ID)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                if attempts < max_attempts:
                    delay = backoff_base * (2 ** (attempts - 1))  # 指数退避
                    print(f"[{task_id}] attempt={attempts} failed: {err} -> retry in {delay}s")
                    queue.requeue_later(msg, attempts, delay, error=err)
                else:
                    print(f"[{task_id}] attempt={attempts} failed permanently: {err} -> dead letter")
                    queue.dead_letter(msg, attempts, error=err, expected_worker=WORKER_ID)
            finally:
                renewer.stop()
                current_task_id = ""
        except KeyboardInterrupt:
            print("Worker stopped by user.")
            break
        except Exception as e:
            print(f"Worker loop error: {e}")
            time.sleep(2)


if __name__ == "__main__":
    main()
