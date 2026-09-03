#!/usr/bin/env bash
set -e
echo "== 1) health caps from worker container =="
sudo docker exec -i maps-bagel-worker python3 - <<'PY'
import requests
h = requests.get("http://host.docker.internal:6006/health", timeout=12).json()
print("status:", h.get("status"))
print("capabilities:", h.get("capabilities"))
print("whisper_status:", h.get("whisper_status"), "| ffmpeg:", h.get("ffmpeg"))
PY

echo "== 2) copy test audio into worker container =="
sudo docker cp /tmp/test_zh.mp3 maps-bagel-worker:/tmp/test_zh.mp3

echo "== 3) POST /v1/transcribe from worker container (with token) =="
sudo docker exec -i maps-bagel-worker python3 - <<'PY'
import requests, os, time
TOKEN = os.environ.get("BAGEL_LOCAL_SERVICE_TOKEN", "")
url = "http://host.docker.internal:6006/v1/transcribe"
headers = {"Authorization": f"Bearer {TOKEN}"}
for i in range(20):
    with open("/tmp/test_zh.mp3", "rb") as f:
        r = requests.post(url, headers=headers,
                          files={"file": ("test_zh.mp3", f, "audio/mpeg")},
                          data={"language": "zh", "beam_size": "5", "vad_filter": "true"},
                          timeout=300)
    if r.status_code == 503:
        print("503 whisper loading, retry 15s..."); time.sleep(15); continue
    print("HTTP", r.status_code)
    d = r.json()
    if r.status_code == 200:
        print("model:", d.get("model"), "| lang:", d.get("language"),
              "| duration:", d.get("duration"), "| infer_sec:", d.get("infer_sec"))
        print("segments:", len(d.get("segments") or []))
        print("TEXT:", (d.get("text") or "").replace("\n", " / "))
    else:
        print(str(d)[:400])
    break
PY
echo "== TRANSCRIBE VERIFY DONE =="
