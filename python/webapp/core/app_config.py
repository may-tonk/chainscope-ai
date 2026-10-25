"""网站后端的运行配置（和数据库连接配置 settings.py 分开）。

| 变量 | 含义 | 开发环境默认值 |
| --- | --- | --- |
| WEBAPP_ENV              | development（开发）或 production（生产） | development |
| WEBAPP_ALLOWED_ORIGINS  | 允许发起修改请求的网站地址，逗号分隔 | http://localhost:5173 |
| WEBAPP_ALLOWED_HOSTS    | 允许的 Host 请求头，逗号分隔，防止伪造主机名 | localhost,127.0.0.1 |
| WEBAPP_MAIL_MODE        | 发信方式；目前只有 console（打印到后端窗口） | console |
| WEBAPP_TRUSTED_PROXIES  | 受信任的反向代理地址（CIDR，逗号分隔）；只有经过它们才读取 X-Forwarded-For | 空 |

生产环境的额外要求（不满足就拒绝启动）：
- 必须明确填写允许的来源和主机，来源必须是 https；
- 不能使用 console 发信（验收 A21：开发用的打印模式不能带到线上）。
"""

import ipaddress
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from dotenv import dotenv_values

from .errors import ConfigError
from .settings import ROOT

ENVIRONMENTS = ("development", "production")
MAIL_MODES = ("console",)
DEV_ORIGINS = ("http://localhost:5173",)
DEV_HOSTS = ("localhost", "127.0.0.1")


@dataclass(frozen=True)
class AppConfig:
    environment: str
    allowed_origins: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    mail_mode: str
    trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def docs_enabled(self) -> bool:
        """接口说明页 /docs 只在开发环境打开。"""
        return not self.is_production


def _list(values: dict, name: str) -> tuple[str, ...]:
    return tuple(
        item.strip() for item in (values.get(name) or "").split(",") if item.strip()
    )


def _origin(value: str) -> str:
    """来源必须是“协议://主机[:端口]”，不能带路径；统一去掉末尾的 /。"""
    parts = urlsplit(value)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.path not in ("", "/")
    ):
        raise ConfigError(f"WEBAPP_ALLOWED_ORIGINS 中的地址格式不正确：{value}")
    return f"{parts.scheme}://{parts.netloc}"


def load_app_config(values: dict | None = None) -> AppConfig:
    if values is None:
        values = {**dotenv_values(ROOT / ".env", interpolate=False), **os.environ}
    environment = (values.get("WEBAPP_ENV") or "development").strip()
    if environment not in ENVIRONMENTS:
        raise ConfigError("WEBAPP_ENV 只能是 development 或 production")
    production = environment == "production"

    origins = tuple(_origin(o) for o in _list(values, "WEBAPP_ALLOWED_ORIGINS"))
    hosts = _list(values, "WEBAPP_ALLOWED_HOSTS")
    if production:
        if not origins or not hosts:
            raise ConfigError(
                "生产环境必须配置 WEBAPP_ALLOWED_ORIGINS 和 WEBAPP_ALLOWED_HOSTS"
            )
        if any(not o.startswith("https://") for o in origins):
            raise ConfigError("生产环境的 WEBAPP_ALLOWED_ORIGINS 必须是 https 地址")
    origins = origins or DEV_ORIGINS
    hosts = hosts or DEV_HOSTS

    mail_mode = (
        values.get("WEBAPP_MAIL_MODE") or ("" if production else "console")
    ).strip()
    if mail_mode not in MAIL_MODES:
        raise ConfigError("WEBAPP_MAIL_MODE 未配置或不支持（目前只有 console）")
    if production and mail_mode == "console":
        raise ConfigError("生产环境不能使用 console 发信模式")

    try:
        proxies = tuple(
            ipaddress.ip_network(item, strict=False)
            for item in _list(values, "WEBAPP_TRUSTED_PROXIES")
        )
    except ValueError:
        raise ConfigError("WEBAPP_TRUSTED_PROXIES 必须是逗号分隔的 IP 或网段") from None
    return AppConfig(environment, origins, hosts, mail_mode, proxies)
