"""
Web 容器启动入口
================
MySQL 容器健康检查（mysqladmin ping）通过后，其内部用户授权初始化可能仍在进行，
此时 web 直接 db.create_all() 会遇到 (2003, Connection refused) 并崩溃重启。
本脚本先等待数据库真正可连接，再启动 waitress，消除启动竞态。
"""
import os
import sys
import time

# 确保项目根目录可导入（以 scripts/start_web.py 方式运行时 sys.path[0] 是 scripts/）
_script_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_script_dir)
if _root not in sys.path:
    sys.path.insert(0, _root)
os.chdir(_root)

from sqlalchemy import create_engine, text

from config import Config


def wait_for_database(max_wait: int = 120) -> None:
    if Config.SQLALCHEMY_DATABASE_URI.startswith("sqlite"):
        return  # SQLite 为本地文件，无需等待
    engine = create_engine(Config.SQLALCHEMY_DATABASE_URI, pool_pre_ping=True)
    deadline = time.time() + max_wait
    last_err = None
    while time.time() < deadline:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            print("[startup] database is ready", flush=True)
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[startup] waiting for database... ({e})", flush=True)
            time.sleep(3)
    print(f"[startup] database not reachable after {max_wait}s: {last_err}", file=sys.stderr, flush=True)
    sys.exit(1)


def main() -> None:
    wait_for_database()
    from waitress import serve

    from app import app

    print("[startup] serving on http://0.0.0.0:5000", flush=True)
    serve(app, host="0.0.0.0", port=5000, threads=8)


if __name__ == "__main__":
    main()
