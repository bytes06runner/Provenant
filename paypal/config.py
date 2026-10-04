"""Load PayPal settings from config/app.yaml and credentials from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"


class ConfigError(RuntimeError):
    """Raised when required configuration or credentials are missing."""


def load_env() -> None:
    load_dotenv(REPO_ROOT / ".env", override=False)


def load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    with path.open() as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping")
    return data


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable {name} (see .env.example)")
    return value


@dataclass(frozen=True)
class Credentials:
    """A REST app's client credentials. repr never shows the secret."""

    label: str
    client_id: str
    client_secret: str

    def __repr__(self) -> str:
        return f"Credentials(label={self.label!r}, client_id={self.client_id[:6]}...)"


@dataclass(frozen=True)
class HttpSettings:
    api_base: str
    timeout_seconds: float
    max_retries: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    token_refresh_margin_seconds: int


def http_settings() -> HttpSettings:
    load_env()
    cfg = load_yaml("app.yaml")["paypal"]
    return HttpSettings(
        api_base=require_env("PAYPAL_API_BASE").rstrip("/"),
        timeout_seconds=float(cfg["http"]["timeout_seconds"]),
        max_retries=int(cfg["http"]["max_retries"]),
        backoff_base_seconds=float(cfg["http"]["backoff_base_seconds"]),
        backoff_max_seconds=float(cfg["http"]["backoff_max_seconds"]),
        token_refresh_margin_seconds=int(cfg["oauth"]["token_refresh_margin_seconds"]),
    )


def merchant_credentials(merchant_key: str) -> Credentials:
    load_env()
    profile = load_yaml(f"merchants/{merchant_key}.yaml")
    prefix = profile["env_prefix"]
    return Credentials(
        label=f"merchant:{merchant_key}",
        client_id=require_env(f"{prefix}_CLIENT_ID"),
        client_secret=require_env(f"{prefix}_CLIENT_SECRET"),
    )


def operator_credentials() -> Credentials:
    load_env()
    return Credentials(
        label="operator",
        client_id=require_env("PAYPAL_OPERATOR_CLIENT_ID"),
        client_secret=require_env("PAYPAL_OPERATOR_CLIENT_SECRET"),
    )
