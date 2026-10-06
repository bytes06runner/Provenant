"""Deploy Provenant to Render (staging by default): database, API, merchant simulator, buyer app.

  python scripts/render_deploy.py [--env staging] [--plan free] [--webhooks]

Idempotent: services that exist are updated (environment, secret files) and redeployed; missing
ones are created from the public GitHub repository. Secrets go only where they are needed:
  API          PayPal and model credentials, the buyer's signing key, the merchant registry
  simulator    the four merchants' signing keys and the registry (no PayPal or model keys)
  buyer app    only the API's public URL
Keys are read-only in deployment (PROVENANT_KEYS_READONLY=1): a service never mints a key.
--webhooks registers each PayPal app's webhook on the API's public URL and stores the ids.
Needs RENDER_API_KEY in .env. Writes nothing secret to disk or stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import dotenv_values  # noqa: E402

from merchants.registry import MerchantRecord, load_records  # noqa: E402
from paypal.client import PayPalClient, PayPalError  # noqa: E402
from paypal.config import (  # noqa: E402
    http_settings,
    load_yaml,
    merchant_credentials,
    operator_credentials,
)

API = "https://api.render.com/v1"
REPO = "https://github.com/bytes06runner/Provenant"
REGION = "oregon"
PYTHON_VERSION = "3.12.7"
NODE_VERSION = "20.18.0"
# Local-only variables that never leave this machine.
LOCAL_ONLY = {
    "RENDER_API_KEY",
    "STAGING_DATABASE_URL",
    "GROQ_API_KEY_B",
    "DATABASE_URL",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "MERCHANTS_BASE_URL",
    "PUBLIC_API_BASE_URL",
    "PAYPAL_RETURN_URL",
    "PAYPAL_CANCEL_URL",
}
PY_BUILD = "pip install --upgrade pip && pip install -e ."


class Render:
    def __init__(self, key: str) -> None:
        self.http = httpx.Client(
            base_url=API,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=60,
        )

    def call(self, method: str, path: str, **kw: Any) -> Any:
        r = self.http.request(method, path, **kw)
        if r.status_code >= 400:
            raise SystemExit(f"Render {method} {path}: HTTP {r.status_code}: {r.text[:500]}")
        return r.json() if r.content else None

    def owner(self) -> str:
        return str(self.call("GET", "/owners", params={"limit": 1})[0]["owner"]["id"])

    def service(self, name: str) -> dict[str, Any] | None:
        for row in self.call("GET", "/services", params={"name": name, "limit": 20}):
            if row["service"]["name"] == name:
                return dict(row["service"])
        return None

    def postgres(self, name: str) -> dict[str, Any] | None:
        for row in self.call("GET", "/postgres", params={"name": name, "limit": 20}):
            if row["postgres"]["name"] == name:
                return dict(row["postgres"])
        return None


def env_list(values: dict[str, str]) -> list[dict[str, str]]:
    return [{"key": k, "value": v} for k, v in sorted(values.items())]


def ensure_service(
    r: Render,
    owner: str,
    name: str,
    *,
    runtime: str,
    build: str,
    start: str,
    health: str,
    env: dict[str, str],
    secret_files: dict[str, str],
    plan: str,
    root_dir: str | None = None,
) -> dict[str, Any]:
    svc = r.service(name)
    if svc is None:
        body: dict[str, Any] = {
            "type": "web_service",
            "name": name,
            "ownerId": owner,
            "repo": REPO,
            "branch": "main",
            "autoDeploy": "no",
            "envVars": env_list(env),
            "secretFiles": [{"name": k, "content": v} for k, v in secret_files.items()],
            "serviceDetails": {
                "runtime": runtime,
                "plan": plan,
                "region": REGION,
                "healthCheckPath": health,
                "envSpecificDetails": {"buildCommand": build, "startCommand": start},
            },
        }
        if root_dir:
            body["rootDir"] = root_dir
        created = r.call("POST", "/services", json=body)
        svc = dict(created["service"])
        print(f"created {name}: {svc['serviceDetails']['url']}")
        return svc
    r.call("PUT", f"/services/{svc['id']}/env-vars", json=env_list(env))
    for fname, content in secret_files.items():
        r.call("PUT", f"/services/{svc['id']}/secret-files/{fname}", json={"content": content})
    r.call(
        "PATCH",
        f"/services/{svc['id']}",
        json={
            "serviceDetails": {"envSpecificDetails": {"buildCommand": build, "startCommand": start}}
        },
    )
    r.call("POST", f"/services/{svc['id']}/deploys", json={"clearCache": "do_not_clear"})
    print(f"updated and redeploying {name}: {svc['serviceDetails']['url']}")
    return svc


def staged_registry(records: dict[str, MerchantRecord], merchants_url: str) -> str:
    """The onboarding registry with storefront URLs pointing at the deployed simulator."""
    doc = {
        "merchants": {
            k: {
                "merchant_id": rec.merchant_id,
                "display_name": rec.display_name,
                "paypal_merchant_id": rec.paypal_merchant_id,
                "key_id": rec.key_id,
                "public_key": rec.public_key,
                "base_url": f"{merchants_url.rstrip('/')}/m/{k}",
            }
            for k, rec in sorted(records.items())
        }
    }
    return json.dumps(doc, indent=1)


def register_webhooks(api_url: str, merchants: list[str]) -> dict[str, str]:
    """One webhook per PayPal app on the API's public URL; returns env vars with the ids."""
    cfg = load_yaml("app.yaml")["webhooks"]["event_types"]
    out = {}
    apps = [("operator", operator_credentials(), cfg["operator"])] + [
        (m, merchant_credentials(m), cfg["merchant"]) for m in merchants
    ]
    for label, creds, types in apps:
        url = f"{api_url.rstrip('/')}/api/webhooks/paypal/{label}"
        with PayPalClient(creds, http_settings()) as c:
            existing = c.get("/v1/notifications/webhooks").body.get("webhooks", [])
            hook = next((w for w in existing if w.get("url") == url), None)
            if hook is None:
                try:
                    hook = c.post(
                        "/v1/notifications/webhooks",
                        json={"url": url, "event_types": [{"name": t} for t in types]},
                    ).body
                except PayPalError as e:
                    print(f"webhook for {label} not registered: {e.summary()}")
                    continue
        out[f"PAYPAL_WEBHOOK_ID_{label.upper()}"] = str(hook["id"])
        print(f"webhook {label}: {hook['id']}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--env", default="staging")
    ap.add_argument("--plan", default="free")
    ap.add_argument("--db", default=None, help="Render Postgres name (default provenant-<env>-db)")
    ap.add_argument("--webhooks", action="store_true")
    args = ap.parse_args()
    local = {k: v for k, v in dotenv_values(REPO_ROOT / ".env").items() if v is not None}
    r = Render(local["RENDER_API_KEY"])
    owner = r.owner()
    names = {s: f"provenant-{s}-{args.env}" for s in ("api", "merchants", "web")}

    db = r.postgres(args.db or f"provenant-{args.env}-db")
    if db is None:
        raise SystemExit("create the Render Postgres first (see docs/deploy.md)")
    internal = r.call("GET", f"/postgres/{db['id']}/connection-info")["internalConnectionString"]

    # URLs are known once the services exist; create them first, then fill in what depends on them.
    def url_of(key: str) -> str | None:
        svc = r.service(names[key])
        return svc["serviceDetails"]["url"] if svc else None

    records = load_records(REPO_ROOT / "var" / "registry.json")
    keys_dir = REPO_ROOT / "var" / "keys"
    merchants_url = url_of("merchants") or f"https://{names['merchants']}.onrender.com"
    registry = staged_registry(records, merchants_url)
    common = {
        "PYTHON_VERSION": PYTHON_VERSION,
        "PROVENANT_KEYS_READONLY": "1",
        "PROVENANT_KEYS_DIR": "/etc/secrets",
        "PROVENANT_USER_KEYS_DIR": "/etc/secrets",
        "PROVENANT_REGISTRY_PATH": "/etc/secrets/registry.json",
    }
    merchants_svc = ensure_service(
        r,
        owner,
        names["merchants"],
        runtime="python",
        build=PY_BUILD,
        start="uvicorn merchants.app:app --host 0.0.0.0 --port $PORT",
        health="/healthz",
        env={**common, "SIMULATOR_ALLOW_PLANTED": "1" if args.env == "staging" else "0"},
        secret_files={
            "registry.json": registry,
            **{f"{m}.ed25519.pem": (keys_dir / f"{m}.ed25519.pem").read_text() for m in records},
        },
        plan=args.plan,
    )
    merchants_url = merchants_svc["serviceDetails"]["url"]
    registry = staged_registry(records, merchants_url)
    web_url = url_of("web") or f"https://{names['web']}.onrender.com"
    api_url = url_of("api") or f"https://{names['api']}.onrender.com"

    api_env = {k: v for k, v in local.items() if k not in LOCAL_ONLY}
    api_env.update(common)
    api_env.update(
        {
            "PROVENANT_DATABASE_URL": internal,
            "MERCHANTS_BASE_URL": merchants_url,
            "PROVENANT_CORS_ORIGINS": web_url,
            "PROVENANT_CLIENT_TOKEN_DOMAINS": web_url.split("://", 1)[-1],
            "PAYPAL_RETURN_URL": f"{web_url}/orders",
            "PAYPAL_CANCEL_URL": f"{web_url}/orders",
            "AUTO_REMEDY": "false",
        }
    )
    if args.webhooks:
        api_env.update(register_webhooks(api_url, sorted(records)))
    else:
        svc = r.service(names["api"])
        if svc is not None:  # keep webhook ids registered by an earlier run
            for row in r.call("GET", f"/services/{svc['id']}/env-vars", params={"limit": 100}):
                k = row["envVar"]["key"]
                if k.startswith("PAYPAL_WEBHOOK_ID_"):
                    api_env[k] = row["envVar"]["value"]
    api_svc = ensure_service(
        r,
        owner,
        names["api"],
        runtime="python",
        build=PY_BUILD,
        start="alembic upgrade head && uvicorn api.app:app --host 0.0.0.0 --port $PORT",
        health="/api/health",
        env=api_env,
        secret_files={
            "registry.json": registry,
            "buyer_a.ed25519.pem": (keys_dir / "users" / "buyer_a.ed25519.pem").read_text(),
        },
        plan=args.plan,
    )
    api_url = api_svc["serviceDetails"]["url"]
    ensure_service(
        r,
        owner,
        names["web"],
        runtime="node",
        root_dir="web",
        build="npm ci && npm run build",
        start="npm run start -- -p $PORT",
        health="/",
        env={"NODE_VERSION": NODE_VERSION, "NEXT_PUBLIC_API_URL": api_url},
        secret_files={},
        plan=args.plan,
    )
    print(json.dumps({"api": api_url, "merchants": merchants_url, "web": web_url}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
