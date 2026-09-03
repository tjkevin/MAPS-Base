# MAPS 云端镜像：仅含 Web 业务 + 队列 Worker 所需的轻量依赖
# 模型推理（torch/whisper/ffmpeg）在 autoDL / 局域网 GPU 机器运行，云端不安装
FROM python:3.11-slim

WORKDIR /app

# 系统依赖：tzdata 使 TZ=Asia/Shanghai 生效（apt 使用阿里云镜像加速）
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && rm -rf /var/lib/apt/lists/*

# 创建非 root 运行用户（uid/gid=1000，与宿主机运行用户 ubuntu 对齐，
# 保证 bind-mount 的 uploads/subtitles/logs 目录在容器内可读写）
RUN groupadd -g 1000 app && useradd -u 1000 -g app -m -s /usr/sbin/nologin app

# 先复制requirements，利用Docker缓存（pip使用阿里云镜像加速）
COPY docs/requirements.txt .
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple -r requirements.txt

# 创建必要目录
RUN mkdir -p uploads subtitles instance logs

# 复制应用代码
COPY . .

# 清理不必要的文件，并把应用目录属主交给非 root 用户
RUN rm -rf venv/ __pycache__/ .git/ ss/ *.db 2>/dev/null || true \
    && chown -R app:app /app

# 设置环境变量
ENV FLASK_APP=app.py \
    FLASK_ENV=production \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 暴露端口
EXPOSE 5000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:5000/login', timeout=5)" || exit 1

# 以非 root 用户运行（uid=1000）
USER app

# 启动命令：先等待MySQL就绪，再用waitress作为生产WSGI服务器
CMD ["python", "scripts/start_web.py"]
