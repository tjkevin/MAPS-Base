# 多阶段构建，减小镜像体积
FROM python:3.11-slim AS builder

WORKDIR /app

# 安装系统依赖（构建阶段）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    ffmpeg \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# 创建虚拟环境
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 升级pip
RUN pip install --no-cache-dir --upgrade pip

# 先复制requirements，利用Docker缓存
COPY docs/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 多模态模型可选依赖
COPY multimodal_models/requirements_mm.txt .
RUN pip install --no-cache-dir -r requirements_mm.txt || true


# 最终运行阶段
FROM python:3.11-slim

WORKDIR /app

# 安装运行时系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# 从builder阶段复制虚拟环境
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 创建必要目录
RUN mkdir -p uploads subtitles instance logs

# 复制应用代码
COPY . .

# 清理不必要的文件
RUN rm -rf venv/ __pycache__/ .git/ ss/ *.db 2>/dev/null || true

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

# 启动命令（使用waitress作为生产WSGI服务器）
CMD ["python", "-c", "from waitress import serve; from app import app; serve(app, host='0.0.0.0', port=5000, threads=8)"]
