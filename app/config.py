"""应用配置：全部通过环境变量覆盖，带默认值。"""
from __future__ import annotations

import os
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# SQLite 数据库文件路径（容器内默认 /data/allergens.db）
DB_PATH = Path(os.environ.get("DB_PATH", "./data/allergens.db"))

# 服务监听
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

# 首次启动且数据库为空时是否写入演示数据
SEED_DEMO = _bool("SEED_DEMO", False)

# 可选：逗号分隔的过敏原白名单；留空表示不限制（声明时大小写不敏感、统一大写）
_raw_allowed = os.environ.get("ALLOWED_ALLERGENS", "").strip()
ALLOWED_ALLERGENS: set[str] | None = (
    {a.strip().upper() for a in _raw_allowed.split(",") if a.strip()}
    if _raw_allowed
    else None
)
