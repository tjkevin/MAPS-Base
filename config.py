import os
from datetime import timedelta
from urllib.parse import quote_plus

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
_SQLITE_PATH = os.path.join(_PROJECT_DIR, 'maps_local.db').replace('\\', '/')

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(_PROJECT_DIR, '.env'))
except ImportError:
    pass


def _mysql_url_from_env() -> str:
    """根据 MYSQL_* 环境变量组装连接串（utf8mb4，适配中文与 emoji 元数据）。"""
    user = os.environ.get('MYSQL_USER', 'maps')
    password = os.environ.get('MYSQL_PASSWORD', 'maps_dev')
    host = os.environ.get('MYSQL_HOST', '127.0.0.1')
    port = os.environ.get('MYSQL_PORT', '3306')
    database = os.environ.get('MYSQL_DATABASE', 'maps')
    pwd = quote_plus(password)
    return (
        f'mysql+pymysql://{user}:{pwd}@{host}:{port}/{database}'
        f'?charset=utf8mb4'
    )


def _database_uri() -> str:
    """
    连接优先级：
    1) DATABASE_URL 显式指定（任意 SQLAlchemy 支持的 URL）
    2) MAPS_USE_SQLITE=1 / true → 本地 SQLite（无 MySQL 时的开发回退）
    3) 默认 → MySQL（由 MYSQL_* 或内置默认值拼装）
    """
    explicit = os.environ.get('DATABASE_URL', '').strip()
    if explicit:
        return explicit
    if os.environ.get('MAPS_USE_SQLITE', '').strip().lower() in (
        '1',
        'true',
        'yes',
        'on',
    ):
        return f'sqlite:///{_SQLITE_PATH}'
    return _mysql_url_from_env()


def _engine_options(uri: str) -> dict:
    if uri.startswith('sqlite'):
        return {'connect_args': {'check_same_thread': False}}
    return {
        'pool_pre_ping': True,
        'pool_recycle': 1800,
        'pool_size': int(os.environ.get('MYSQL_POOL_SIZE', '10')),
        'max_overflow': int(os.environ.get('MYSQL_MAX_OVERFLOW', '20')),
    }


_RESOLVED_DB_URI = _database_uri()


class Config:
    # f4 安全：生产（MySQL）部署必须显式配置 SECRET_KEY，缺失即启动失败；
    # 仅本地 SQLite 开发模式允许使用内存随机回退（每次重启会话失效，仅开发用）。
    SECRET_KEY = os.environ.get('SECRET_KEY')
    if not SECRET_KEY:
        if _RESOLVED_DB_URI.startswith('sqlite'):
            import secrets as _secrets
            SECRET_KEY = 'dev-only-' + _secrets.token_hex(16)
        else:
            raise RuntimeError(
                'SECRET_KEY 环境变量未设置：生产（MySQL）部署必须配置强随机 SECRET_KEY，'
                '例如执行 openssl rand -hex 32 生成后写入 .env'
            )
    SESSION_COOKIE_SECURE = os.environ.get('SESSION_COOKIE_SECURE', 'false').strip().lower() in ('1', 'true', 'yes', 'on')
    SESSION_COOKIE_HTTPONLY = True
    # f4 安全：Lax 防 CSRF（跨站跳转不带 cookie），HTTPS 上线后可设 SESSION_COOKIE_SECURE=1
    SESSION_COOKIE_SAMESITE = os.environ.get('SESSION_COOKIE_SAMESITE', 'Lax').strip() or 'Lax'
    SESSION_IDLE_TIMEOUT = int(os.environ.get('SESSION_IDLE_TIMEOUT') or 1800)
    PERMANENT_SESSION_LIFETIME = timedelta(
        seconds=int(os.environ.get('PERMANENT_SESSION_LIFETIME') or 1800)
    )
    SQLALCHEMY_DATABASE_URI = _RESOLVED_DB_URI
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = _engine_options(_RESOLVED_DB_URI)

    REDIS_URL = os.environ.get('REDIS_URL') or 'redis://localhost:6379/0'
    BAGEL_QUEUE_NAME = os.environ.get('BAGEL_QUEUE_NAME') or 'bagel:task:queue'
    BAGEL_TASK_KEY_PREFIX = os.environ.get('BAGEL_TASK_KEY_PREFIX') or 'bagel:task:'
    BAGEL_RESULT_TTL_SECONDS = int(os.environ.get('BAGEL_RESULT_TTL_SECONDS') or 86400)
    BAGEL_SERVICE_URL = os.environ.get('BAGEL_SERVICE_URL') or 'http://bagel-service:8000'

    # ---------- 模型服务多后端（云端不跑模型，统一调用外部API）----------
    # 后端类型：autodl(BAGEL满血版,autoDL租用卡) | local(BAGEL优化版,局域网GPU)
    #          | deepseek(云端大模型API,纯文本) | auto(按任务类型自动路由)
    #          | bagel(旧值,等同默认GPU端点)
    MODEL_DEFAULT_BACKEND = (os.environ.get('MODEL_DEFAULT_BACKEND') or 'auto').strip().lower()
    # auto 路由时图片/媒体任务默认使用的 GPU 端点：autodl | local
    MODEL_DEFAULT_GPU_BACKEND = (os.environ.get('MODEL_DEFAULT_GPU_BACKEND') or 'local').strip().lower()

    # GPU 推理服务端点（协议由 BAGEL_<TARGET>_PROTOCOL 决定，默认 autodl=autodl_v1、local=lan）
    # autoDL：BAGEL SFT 满血版（serve_api.py，单图 VQA；sft_nf4/sft_bf16 两形态）
    #   地址三选一（详见 docs/AutoDL对接配置说明-20260903.md）：
    #   - AutoDL 自定义服务公网 HTTPS：https://<region>-<instance>.autodl.com
    #   - SSH 反向隧道（推荐生产）：容器内填 http://host.docker.internal:18080
    #   - frp 穿透：http://<腾讯云公网IP>:18080
    BAGEL_AUTODL_SERVICE_URL = (os.environ.get('BAGEL_AUTODL_SERVICE_URL') or '').rstrip('/')
    # Bearer Key：serve_api.py 启动时生成于 AutoDL 的 logs/api_key.txt（或环境变量 BAGEL_API_KEY）
    BAGEL_AUTODL_SERVICE_TOKEN = os.environ.get('BAGEL_AUTODL_SERVICE_TOKEN') or ''
    # 局域网本地机：BAGEL NF4 优化版（轻量快速，图片理解 + Whisper 转写/抽帧）
    BAGEL_LOCAL_SERVICE_URL = (os.environ.get('BAGEL_LOCAL_SERVICE_URL') or '').rstrip('/')
    BAGEL_LOCAL_SERVICE_TOKEN = os.environ.get('BAGEL_LOCAL_SERVICE_TOKEN') or ''

    # 自建GPU推理服务（BAGEL协议：POST /infer）——旧的单一端点，作为 autodl/local 未配置时的回落
    BAGEL_SERVICE_TOKEN = os.environ.get('BAGEL_SERVICE_TOKEN') or ''
    # 文件传输方式：upload=worker以multipart推送文件；url=推理服务凭file_download_url回云端拉取
    BAGEL_FILE_TRANSFER = (os.environ.get('BAGEL_FILE_TRANSFER') or 'upload').strip().lower()

    # GPU 后端通信协议：
    #   lan        = 局域网 NF4 优化版 bagel_api.py（GET /health 免鉴权 + POST /v1/understand
    #                Bearer 鉴权 + JSON base64 图片；图片理解 + Whisper 转写/抽帧）
    #   autodl_v1  = autoDL 2026-09 版 serve_api.py（GET /health 返回 waiting_gpu/loading/ready/error
    #                + POST /v1/infer，JSON base64 单图问答，模型形态 sft_nf4/sft_bf16 可切换）
    #   infer      = 旧版 BAGEL 协议（POST /infer，multipart 推送文件，全模态，仅旧服务兼容）
    # 局域网优化版默认 lan；autoDL 默认 autodl_v1（仓库 autodl_api/serve_api.py）。
    BAGEL_LOCAL_PROTOCOL = (os.environ.get('BAGEL_LOCAL_PROTOCOL') or 'lan').strip().lower()
    BAGEL_AUTODL_PROTOCOL = (os.environ.get('BAGEL_AUTODL_PROTOCOL') or 'autodl_v1').strip().lower()
    # 局域网 API 调用参数：图片理解单次超时（秒，理解任务通常 1.3~30s，留足余量）
    BAGEL_LAN_UNDERSTAND_TIMEOUT = int(os.environ.get('BAGEL_LAN_UNDERSTAND_TIMEOUT') or 300)
    # 局域网 API 健康检查超时（秒）
    BAGEL_LAN_HEALTH_TIMEOUT = float(os.environ.get('BAGEL_LAN_HEALTH_TIMEOUT') or 8)
    # autoDL serve_api.py 调用参数：
    # 模型形态：sft_nf4（量化省显存、常驻~8.8GiB、加载快）/ sft_bf16（满血精度、~27.2GiB、切换约10-20s）；
    # 任务级可用 options.autodl_model 覆盖
    BAGEL_AUTODL_MODEL = (os.environ.get('BAGEL_AUTODL_MODEL') or 'sft_nf4').strip()
    # /v1/infer 单次超时（秒；单题 0.8~3s，模型切换/加载最长约 20s，留足余量）
    BAGEL_AUTODL_V1_TIMEOUT = int(os.environ.get('BAGEL_AUTODL_V1_TIMEOUT') or 300)
    # autoDL 健康检查超时（秒，公网入口略慢于内网）
    BAGEL_AUTODL_HEALTH_TIMEOUT = float(os.environ.get('BAGEL_AUTODL_HEALTH_TIMEOUT') or 8)
    # 外部worker（autoDL/局域网）下载云端文件的基地址与共享Token；留空则外部worker无法取文件
    MODEL_FILE_BASE_URL = (os.environ.get('MODEL_FILE_BASE_URL') or '').rstrip('/')
    MODEL_FILE_TOKEN = os.environ.get('MODEL_FILE_TOKEN') or ''

    # DeepSeek API（OpenAI兼容，无需GPU；管理员设置用量上限）
    DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY') or ''
    DEEPSEEK_BASE_URL = (os.environ.get('DEEPSEEK_BASE_URL') or 'https://api.deepseek.com').rstrip('/')
    DEEPSEEK_MODEL = os.environ.get('DEEPSEEK_MODEL') or 'deepseek-chat'
    DEEPSEEK_REQUEST_TIMEOUT = int(os.environ.get('DEEPSEEK_REQUEST_TIMEOUT') or 120)
    # 配额：0 表示不限制；按 token 用量在 Redis 按日/月累计
    DEEPSEEK_DAILY_TOKEN_LIMIT = int(os.environ.get('DEEPSEEK_DAILY_TOKEN_LIMIT') or 0)
    DEEPSEEK_MONTHLY_TOKEN_LIMIT = int(os.environ.get('DEEPSEEK_MONTHLY_TOKEN_LIMIT') or 0)

    # ---------- 按人算力积分（DeepSeek token 与 GPU 算力统一计量扣减）----------
    # 总开关；关闭后不预检、不扣减（管理员尚未发放任何积分时也自动不拦截）
    CREDIT_ENABLED = os.environ.get('CREDIT_ENABLED', '1').strip().lower() in ('1', 'true', 'yes', 'on')
    # DeepSeek：每 1000 token 消耗积分
    CREDIT_DEEPSEEK_PER_1K = float(os.environ.get('CREDIT_DEEPSEEK_PER_1K') or 1)
    # GPU：每次任务基础积分 + 每分钟媒体时长积分（autoDL 满血版按租用成本计价，local 优化版为沉没成本）
    CREDIT_AUTODL_BASE = int(os.environ.get('CREDIT_AUTODL_BASE') or 10)
    CREDIT_AUTODL_PER_MIN = float(os.environ.get('CREDIT_AUTODL_PER_MIN') or 3)
    CREDIT_LOCAL_BASE = int(os.environ.get('CREDIT_LOCAL_BASE') or 2)
    CREDIT_LOCAL_PER_MIN = float(os.environ.get('CREDIT_LOCAL_PER_MIN') or 1)
    # 媒体时长未知时的保守预估秒数（提交冻结用）
    CREDIT_EST_VIDEO_SECONDS = int(os.environ.get('CREDIT_EST_VIDEO_SECONDS') or 600)
    CREDIT_EST_AUDIO_SECONDS = int(os.environ.get('CREDIT_EST_AUDIO_SECONDS') or 300)
    # 同文件（MD5）算法结果复用：命中缓存免积分
    CREDIT_CACHE_HIT_FREE = os.environ.get('CREDIT_CACHE_HIT_FREE', '1').strip().lower() in ('1', 'true', 'yes', 'on')

    # ---------- 反馈#10：Whisper 转写 / 关键帧简述 / 外部大模型视频描述 ----------
    # Whisper 转写（局域网 GPU 执行，成本低）：基础积分 + 每分钟媒体时长积分
    CREDIT_TRANSCRIBE_BASE = int(os.environ.get('CREDIT_TRANSCRIBE_BASE') or 1)
    CREDIT_TRANSCRIBE_PER_MIN = float(os.environ.get('CREDIT_TRANSCRIBE_PER_MIN') or 1)
    # BAGEL 关键帧简述（视频，逐帧理解）：基础积分 + 每帧积分
    CREDIT_KEYFRAMES_BASE = int(os.environ.get('CREDIT_KEYFRAMES_BASE') or 2)
    CREDIT_KEYFRAMES_PER_FRAME = float(os.environ.get('CREDIT_KEYFRAMES_PER_FRAME') or 2)
    # 关键帧抽帧密度（预估积分用）：默认每 8 秒一帧、单次最多 6 帧（与 GPU 端默认一致）
    CREDIT_KEYFRAME_INTERVAL_SEC = float(os.environ.get('CREDIT_KEYFRAME_INTERVAL_SEC') or 8)
    CREDIT_KEYFRAME_MAX_FRAMES = int(os.environ.get('CREDIT_KEYFRAME_MAX_FRAMES') or 6)
    # 局域网 API 媒体端点超时/上传上限（转写与抽帧为长任务，默认 30 分钟）
    BAGEL_LAN_TRANSCRIBE_TIMEOUT = int(os.environ.get('BAGEL_LAN_TRANSCRIBE_TIMEOUT') or 1800)
    BAGEL_LAN_KEYFRAMES_TIMEOUT = int(os.environ.get('BAGEL_LAN_KEYFRAMES_TIMEOUT') or 1800)
    BAGEL_LAN_MEDIA_MAX_MB = int(os.environ.get('BAGEL_LAN_MEDIA_MAX_MB') or 300)

    # ---------- GPU 后端健康检查 / 故障自动切换 ----------
    GPU_HEALTH_TIMEOUT = float(os.environ.get('GPU_HEALTH_TIMEOUT') or 5)
    GPU_HEALTH_CACHE_SECONDS = int(os.environ.get('GPU_HEALTH_CACHE_SECONDS') or 60)
    GPU_FAILOVER_ENABLED = os.environ.get('GPU_FAILOVER_ENABLED', '1').strip().lower() in ('1', 'true', 'yes', 'on')

    # ---------- Worker 重试 / 死信 / 心跳（Redis 队列）----------
    BAGEL_MAX_ATTEMPTS = int(os.environ.get('BAGEL_MAX_ATTEMPTS') or 3)
    BAGEL_RETRY_BACKOFF_SECONDS = int(os.environ.get('BAGEL_RETRY_BACKOFF_SECONDS') or 60)
    BAGEL_WORKER_TTL_SECONDS = int(os.environ.get('BAGEL_WORKER_TTL_SECONDS') or 90)
    BAGEL_DELAYED_ZSET = os.environ.get('BAGEL_DELAYED_ZSET') or 'bagel:task:delayed'
    BAGEL_DEAD_QUEUE_NAME = os.environ.get('BAGEL_DEAD_QUEUE_NAME') or 'bagel:task:dead'
    BAGEL_WORKER_PREFIX = os.environ.get('BAGEL_WORKER_PREFIX') or 'bagel:worker:'
    # 卡死任务恢复阈值：queued 超过该秒数无人消费 / running 超过该秒数无 worker 心跳
    BAGEL_STUCK_QUEUED_SECONDS = int(os.environ.get('BAGEL_STUCK_QUEUED_SECONDS') or 600)
    BAGEL_STUCK_RUNNING_SECONDS = int(os.environ.get('BAGEL_STUCK_RUNNING_SECONDS') or 1800)

    # ---------- 登录限流（按 IP 固定窗口，防爆破；账号锁定逻辑另见 auth_security）----------
    LOGIN_RATE_LIMIT_MAX = int(os.environ.get('LOGIN_RATE_LIMIT_MAX') or 20)
    LOGIN_RATE_WINDOW_SECONDS = int(os.environ.get('LOGIN_RATE_WINDOW_SECONDS') or 300)

    # ---------- 文件存储后端：local（本地/挂载盘）| oss（阿里云OSS/兼容S3）----------
    # 注意：oss 目前为【预留扩展桩】（services/storage.py 已实现但业务上传/下载链路尚未接线），
    # 设为 oss 不会生效，文件仍写入本地 UPLOAD_FOLDER。正式启用 OSS 前请勿在生产配置 oss。
    STORAGE_BACKEND = (os.environ.get('STORAGE_BACKEND') or 'local').strip().lower()
    OSS_ENDPOINT = os.environ.get('OSS_ENDPOINT') or ''
    OSS_BUCKET_NAME = os.environ.get('OSS_BUCKET_NAME') or ''
    OSS_ACCESS_KEY_ID = os.environ.get('OSS_ACCESS_KEY_ID') or ''
    OSS_ACCESS_KEY_SECRET = os.environ.get('OSS_ACCESS_KEY_SECRET') or ''
    OSS_PUBLIC_BASE_URL = (os.environ.get('OSS_PUBLIC_BASE_URL') or '').rstrip('/')

    UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
    MAX_CONTENT_LENGTH = 4 * 1024 * 1024 * 1024
    ALLOWED_EXTENSIONS = {
        'mp4', 'avi', 'mov', 'mkv', 'webm', 'flv',
        'mp3', 'wav', 'flac', 'm4a',
        'jpg', 'jpeg', 'png', 'webp', 'gif',
    }

    CHUNK_SIZE = 5 * 1024 * 1024

    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)
