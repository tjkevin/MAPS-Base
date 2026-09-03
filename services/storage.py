"""文件存储抽象层。

- local（默认）：文件保存在本地 UPLOAD_FOLDER（云主机磁盘或挂载的数据盘），
  经现有 /static 与 /model-files 端点提供访问，零额外依赖。
- oss（扩展桩）：阿里云 OSS / S3 兼容对象存储，适合磁盘紧张或多机共享场景；
  需 ``pip install oss2`` 并配置 OSS_ENDPOINT / OSS_BUCKET_NAME / OSS_ACCESS_KEY_ID /
  OSS_ACCESS_KEY_SECRET 环境变量。未安装 oss2 或配置缺失时给出明确报错。

设计原则：调用方只面向 ``StorageBackend`` 接口（put / get / exists / delete / url），
切换后端只需改 STORAGE_BACKEND 环境变量，业务代码不变。
"""
import os
import shutil
from datetime import timedelta


class StorageBackend:
    """存储后端统一接口。key 为相对路径，如 'recordings/2026/abc.mp4'。"""

    def put(self, key, src_path):
        """把本地文件 src_path 存入存储，返回可访问 URL（本地后端返回 None）。"""
        raise NotImplementedError

    def get(self, key, dst_path):
        """把存储中的文件取回本地 dst_path；返回 dst_path；不存在返回 None。"""
        raise NotImplementedError

    def exists(self, key):
        raise NotImplementedError

    def delete(self, key):
        raise NotImplementedError

    def url(self, key, expires=3600):
        """返回访问 URL；本地后端返回 None（由 Web 路由提供）。"""
        return None


class LocalStorage(StorageBackend):
    def __init__(self, base_dir):
        self.base_dir = os.path.abspath(base_dir)
        os.makedirs(self.base_dir, exist_ok=True)

    def _abs(self, key):
        # 防目录穿越：规范化后必须仍在 base_dir 内
        dest = os.path.normpath(os.path.join(self.base_dir, key))
        if not dest.startswith(self.base_dir + os.sep) and dest != self.base_dir:
            raise ValueError('非法存储路径: %s' % key)
        return dest

    def put(self, key, src_path):
        dest = self._abs(key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.abspath(src_path) != dest:
            shutil.copyfile(src_path, dest)
        return None

    def get(self, key, dst_path):
        src = self._abs(key)
        if not os.path.exists(src):
            return None
        os.makedirs(os.path.dirname(os.path.abspath(dst_path)), exist_ok=True)
        shutil.copyfile(src, dst_path)
        return dst_path

    def exists(self, key):
        return os.path.exists(self._abs(key))

    def delete(self, key):
        path = self._abs(key)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False


class OssStorage(StorageBackend):
    """阿里云 OSS 后端（扩展桩）：依赖可选包 oss2，未安装时抛出明确错误。"""

    def __init__(self, endpoint, bucket_name, key_id, key_secret, public_base_url=''):
        if not (endpoint and bucket_name and key_id and key_secret):
            raise RuntimeError('STORAGE_BACKEND=oss 需要配置 OSS_ENDPOINT/OSS_BUCKET_NAME/'
                               'OSS_ACCESS_KEY_ID/OSS_ACCESS_KEY_SECRET')
        try:
            import oss2  # noqa: F401  延迟导入，local 后端无需安装
        except ImportError as e:
            raise RuntimeError('使用 OSS 存储需先安装依赖：pip install oss2') from e
        import oss2
        self._oss2 = oss2
        auth = oss2.Auth(key_id, key_secret)
        self.bucket = oss2.Bucket(auth, endpoint, bucket_name)
        self.public_base_url = (public_base_url or '').rstrip('/')

    def put(self, key, src_path):
        self.bucket.put_object_from_file(key, src_path)
        return self.url(key)

    def get(self, key, dst_path):
        if not self.exists(key):
            return None
        os.makedirs(os.path.dirname(os.path.abspath(dst_path)), exist_ok=True)
        self.bucket.get_object_to_file(key, dst_path)
        return dst_path

    def exists(self, key):
        return self.bucket.object_exists(key)

    def delete(self, key):
        self.bucket.delete_object(key)
        return True

    def url(self, key, expires=3600):
        if self.public_base_url:
            return '%s/%s' % (self.public_base_url, key.lstrip('/'))
        # 私有桶：生成带签名的临时下载 URL
        return self.bucket.sign_url('GET', key, int(expires), slash_safe=True)


_backend = None


def get_storage(cfg=None):
    """按配置返回存储后端单例。cfg 为 Flask app.config；无配置时读环境变量。"""
    global _backend
    if _backend is not None:
        return _backend
    backend_name = 'local'
    upload_folder = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'uploads')
    if cfg is not None:
        backend_name = (cfg.get('STORAGE_BACKEND') or 'local').lower()
        upload_folder = cfg.get('UPLOAD_FOLDER') or upload_folder
    else:
        backend_name = (os.environ.get('STORAGE_BACKEND') or 'local').lower()
    if backend_name == 'oss':
        _backend = OssStorage(
            endpoint=(cfg.get('OSS_ENDPOINT') if cfg else os.environ.get('OSS_ENDPOINT')) or '',
            bucket_name=(cfg.get('OSS_BUCKET_NAME') if cfg else os.environ.get('OSS_BUCKET_NAME')) or '',
            key_id=(cfg.get('OSS_ACCESS_KEY_ID') if cfg else os.environ.get('OSS_ACCESS_KEY_ID')) or '',
            key_secret=(cfg.get('OSS_ACCESS_KEY_SECRET') if cfg else os.environ.get('OSS_ACCESS_KEY_SECRET')) or '',
            public_base_url=(cfg.get('OSS_PUBLIC_BASE_URL') if cfg else os.environ.get('OSS_PUBLIC_BASE_URL')) or '',
        )
    else:
        _backend = LocalStorage(upload_folder)
    return _backend


def store_file(key, src_path, cfg=None):
    """便捷方法：把本地文件存入当前存储后端，返回 URL（可能为 None）。"""
    return get_storage(cfg).put(key, src_path)


def fetch_file(key, dst_path, cfg=None):
    """便捷方法：从当前存储后端取回文件到本地。"""
    return get_storage(cfg).get(key, dst_path)


def presigned_url(key, expires=timedelta(hours=1), cfg=None):
    """生成临时访问 URL（仅 OSS 私有桶有意义；本地返回 None）。"""
    secs = int(expires.total_seconds()) if isinstance(expires, timedelta) else int(expires)
    return get_storage(cfg).url(key, secs)
