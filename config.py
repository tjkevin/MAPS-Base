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
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'your-secret-key-here'
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
