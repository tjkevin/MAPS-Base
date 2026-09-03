#!/usr/bin/env bash
# 云端侧链路验证（经隧道回连家里 Windows 的 bagel_api）
echo "=== 1) 云宿主监听 6006 情况 ==="
ss -ltn | grep 6006 || echo "6006 NOT listening"

echo "=== 2) 云宿主 -> 隧道 -> Windows: /health ==="
curl -s --max-time 15 http://127.0.0.1:6006/health
echo

echo "=== 3) Docker maps-web 容器经 host.docker.internal: /health ==="
sudo docker exec -i maps-web python3 - <<'PY'
import requests
try:
    r = requests.get("http://host.docker.internal:6006/health", timeout=12)
    print("HTTP", r.status_code, r.json())
except Exception as e:
    print("ERR", type(e).__name__, e)
PY

echo "=== 4) maps-bagel-worker 容器带 Bearer Token: /v1/models ==="
sudo docker exec -i maps-bagel-worker python3 - <<'PY'
import requests
try:
    r = requests.get("http://host.docker.internal:6006/v1/models",
                     headers={"Authorization": "Bearer DiBRFgYRT9ei4XzHoRMn5Efyt4y283A8"}, timeout=12)
    print("HTTP", r.status_code, r.text[:300])
except Exception as e:
    print("ERR", type(e).__name__, e)
PY

echo "=== 5) maps-bagel-worker 容器错误 Token 应 401 ==="
sudo docker exec -i maps-bagel-worker python3 - <<'PY'
import requests
try:
    r = requests.get("http://host.docker.internal:6006/v1/models",
                     headers={"Authorization": "Bearer WRONG"}, timeout=12)
    print("HTTP", r.status_code, "(expect 401)")
except Exception as e:
    print("ERR", type(e).__name__, e)
PY
echo "=== verify done ==="
