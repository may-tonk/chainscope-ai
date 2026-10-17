"""Read private local settings without putting credentials in repr/logs."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from sqlalchemy import URL

from .errors import ChainScopeError

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    rpc_url: str = field(repr=False)
    mysql_host: str
    mysql_port: int
    mysql_user: str = field(repr=False)
    mysql_password: str = field(repr=False)
    mysql_database: str
    # Optional CoinGecko demo key; the public endpoint works without one.
    coingecko_key: str = field(default="", repr=False)
    # Optional SQD Portal key (Testing or paid plan) and Portal address.
    sqd_api_key: str = field(default="", repr=False)
    sqd_portal_url: str = ""

    def require_rpc(self) -> str:
        parts = urlsplit(self.rpc_url)
        if parts.scheme != "https" or not parts.hostname or parts.username:
            raise ChainScopeError("请在项目 .env 配置有效的 HTTPS BSC_RPC_URL")
        return self.rpc_url

    def database_url(self, *, server_only: bool = False) -> URL:
        if not self.mysql_user:
            raise ChainScopeError("请在项目 .env 配置 MYSQL_USER / MYSQL_PASSWORD")
        return URL.create(
            "mysql+pymysql",
            username=self.mysql_user,
            password=self.mysql_password,
            host=self.mysql_host,
            port=self.mysql_port,
            database=None if server_only else self.mysql_database,
            query={"charset": "utf8mb4"},
        )


def load_settings() -> Settings:
    values = {**dotenv_values(ROOT / ".env", interpolate=False), **os.environ}
    try:
        port = int(values.get("MYSQL_PORT") or "3306")
    except ValueError:
        raise ChainScopeError("MYSQL_PORT 必须是整数") from None
    database = values.get("MYSQL_DATABASE") or "chainscope"
    if not 1 <= port <= 65535 or not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_]{0,63}", database
    ):
        raise ChainScopeError("MySQL 端口或数据库名格式不正确")
    return Settings(
        values.get("BSC_RPC_URL") or "",
        values.get("MYSQL_HOST") or "127.0.0.1",
        port,
        values.get("MYSQL_USER") or "",
        values.get("MYSQL_PASSWORD") or "",
        database,
        values.get("COINGECKO_API_KEY") or "",
        values.get("SQD_API_KEY") or "",
        values.get("SQD_PORTAL_URL") or "",
    )
