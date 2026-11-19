"""运行期配置。

数据库路径由环境变量 ``MARKET_DB_PATH`` 决定，默认写入 ``/data/market.db``
（Docker Compose 挂载的卷），本地自测可指向临时文件。
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DB_PATH = "/data/market.db"


def db_path() -> str:
    return os.environ.get("MARKET_DB_PATH", DEFAULT_DB_PATH)


def ensure_parent_dir(path: str | None = None) -> None:
    """确保 SQLite 文件所在目录存在（容器内 /data 由卷挂载，本地自测可能没有）。"""
    target = Path(path or db_path())
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
