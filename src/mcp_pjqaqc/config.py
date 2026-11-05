"""Connection settings, using the same MYSQL_* names as the PJQAQC backend."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parents[2]
REQUIRED = ("MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE")


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    user: str
    database: str
    password: str = field(repr=False)


def _env_file() -> Path:
    explicit = os.environ.get("PJQAQC_ENV_FILE")
    if not explicit:
        return REPO_ROOT / ".env"
    path = Path(explicit)
    if not path.is_file():
        raise ConfigError(f"PJQAQC_ENV_FILE 指向的檔案不存在：{path}")
    return path


def load_config() -> DbConfig:
    path = _env_file()
    values = dict(dotenv_values(path)) if path.is_file() else {}
    # Real environment variables win over the file.
    values.update({k: v for k, v in os.environ.items() if k.startswith("MYSQL_")})

    missing = [key for key in REQUIRED if not values.get(key)]
    if missing:
        raise ConfigError(
            f"缺少資料庫設定：{', '.join(missing)}。請建立 .env（參考 .env.example）"
            "或設定 PJQAQC_ENV_FILE。查詢資料庫結構的工具不需要連線仍可使用。"
        )
    return DbConfig(
        host=values["MYSQL_HOST"],
        port=int(values.get("MYSQL_PORT") or 3306),
        user=values["MYSQL_USER"],
        password=values["MYSQL_PASSWORD"],
        database=values["MYSQL_DATABASE"],
    )
