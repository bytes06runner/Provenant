"""Role configuration: which provider and model serve each role, from env plus config/llm.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from llm.budget import Limits

REPO_ROOT = Path(__file__).resolve().parent.parent


class CachePolicy(StrEnum):
    ALWAYS = "always"
    NEVER = "never"


class LLMConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    provider: str
    model: str

    def label(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass(frozen=True)
class LLMConfig:
    providers: dict[str, dict[str, Any]]
    chains: dict[str, tuple[Target, ...]]
    cache: dict[str, CachePolicy]
    limits: dict[str, Limits]
    default_limits: Limits
    http: dict[str, Any]
    calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    warning_fraction: Decimal = Decimal("0.8")

    def call_defaults(self, role: str) -> dict[str, Any]:
        return dict(self.calls.get(role, {}))

    def chain(self, role: str) -> tuple[Target, ...]:
        try:
            return self.chains[role]
        except KeyError:
            raise LLMConfigError(f"unknown LLM role {role!r}") from None


def _limits(d: dict[str, Any]) -> Limits:
    return Limits(
        tokens_per_minute=int(d["tokens_per_minute"]),
        requests_per_day=int(d["requests_per_day"]),
        tokens_per_day=int(d["tokens_per_day"]),
    )


def load_llm_config(yaml_path: Path | None = None, env: dict[str, str] | None = None) -> LLMConfig:
    if env is None:
        load_dotenv(REPO_ROOT / ".env", override=False)
        env = dict(os.environ)
    path = yaml_path or REPO_ROOT / "config" / "llm.yaml"
    cfg = yaml.safe_load(path.read_text())
    providers = cfg["providers"]

    chains: dict[str, tuple[Target, ...]] = {}
    for role in cfg["roles"]:
        key = role.upper()
        chain = []
        for prefix in (f"LLM_{key}", f"LLM_{key}_FALLBACK"):
            provider = env.get(f"{prefix}_PROVIDER", "").strip()
            model = env.get(f"{prefix}_MODEL", "").strip()
            if not provider and not model:
                continue
            if not provider or not model:
                raise LLMConfigError(f"{prefix}_PROVIDER and {prefix}_MODEL must both be set")
            if provider not in providers:
                raise LLMConfigError(f"{prefix}_PROVIDER={provider!r} is not in config/llm.yaml")
            chain.append(Target(provider, model))
        if not chain:
            raise LLMConfigError(f"no model configured for role {role!r} (LLM_{key}_MODEL)")
        chains[role] = tuple(chain)

    budgets = cfg["budgets"]
    return LLMConfig(
        providers=providers,
        chains=chains,
        cache={r: CachePolicy(cfg["cache"].get(r, "never")) for r in cfg["roles"]},
        limits={m: _limits(v) for m, v in (budgets.get("models") or {}).items()},
        default_limits=_limits(budgets["default"]),
        http=cfg["http"],
        calls=cfg.get("calls") or {},
        warning_fraction=Decimal(str(cfg.get("budget_warning_fraction", "0.8"))),
    )


def api_key(config: LLMConfig, provider: str, env: dict[str, str] | None = None) -> str:
    env_name = config.providers[provider]["api_key_env"]
    value = (env if env is not None else os.environ).get(env_name, "").strip()
    if not value:
        raise LLMConfigError(f"{env_name} is not set (see .env.example)")
    return value
