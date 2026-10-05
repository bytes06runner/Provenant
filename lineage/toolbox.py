"""The real Toolbox behind plans: registered merchants, verified manifests, restricted page
fetching, and the Q-LLM extractor and ranker.

Page fetching is fail-closed. A URL is fetched only if its scheme, host and port equal a
registered merchant's storefront and its normalized path lies under that merchant's
`/products/` or `/reviews/`. Anything else (other hosts, `..` tricks, the storefront's own
manifest or fulfillment endpoints, query strings) is refused and the plan stops. A plan cannot
make the agent visit arbitrary sites, and pages it does read are stored in the Flight Recorder
as content-addressed snapshots.
"""

from __future__ import annotations

import hashlib
import posixpath
from collections.abc import Callable
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import httpx

from lineage.interpreter import InterpreterError
from lineage.manifest import ManifestError, MerchantKeyRegistry, VerifiedManifest, verify_manifest
from lineage.signing import SignatureError, SignedEnvelope
from llm import roles

PAGE_SECTIONS = ("products", "reviews")


class PageRefused(InterpreterError):
    pass


class _Text(HTMLParser):
    """Visible-and-hidden text of a page, as a naive agent would read it from the DOM."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    return "\n".join(p.parts)


class HttpToolbox:
    def __init__(
        self,
        *,
        merchants: dict[str, str],  # merchant_id -> storefront base URL (from the registry)
        keys: MerchantKeyRegistry,
        router: roles.Router,
        http: httpx.Client,
        put_blob: Callable[[bytes, str], str] | None = None,
        record: Callable[[str, dict[str, Any]], None] | None = None,
        seed: int | None = None,
    ) -> None:
        self.merchants = dict(merchants)
        self.keys = keys
        self.router = router
        self.http = http
        self.put_blob = put_blob
        self.record = record or (lambda _t, _p: None)
        self.seed = seed

    # ---- merchants and manifests ---------------------------------------------------

    def list_merchants(self) -> list[str]:
        return sorted(self.merchants)

    def fetch_manifest(self, merchant_id: str) -> VerifiedManifest:
        base = self.merchants.get(merchant_id)
        if base is None:
            raise ManifestError(f"merchant {merchant_id!r} is not registered")
        try:
            resp = self.http.get(f"{base}/.well-known/provenant-manifest.json")
            resp.raise_for_status()
            envelope = SignedEnvelope.from_dict(resp.json())
        except (httpx.HTTPError, ValueError, SignatureError) as e:
            raise ManifestError(f"could not fetch manifest for {merchant_id}: {e}") from e
        manifest = verify_manifest(envelope, merchant_id=merchant_id, registry=self.keys)
        self.record(
            "tool.manifest_verified",
            {"merchant_id": merchant_id, "manifest_hash": manifest.hash, "key_id": envelope.key_id},
        )
        return manifest

    # ---- pages ---------------------------------------------------------------------

    def allowed(self, url: str) -> bool:
        try:
            u = urlsplit(url)
        except ValueError:
            return False
        if u.query or u.fragment or u.username or u.password:
            return False
        path = posixpath.normpath(u.path) if u.path else ""
        # We check the normalized path but fetch the raw one, so they must be identical:
        # no "..", ".", doubled or trailing slashes that a server might resolve differently.
        if "/.." in u.path or path != u.path:
            return False
        for base in self.merchants.values():
            b = urlsplit(base)
            if (u.scheme, u.hostname, u.port) != (b.scheme, b.hostname, b.port):
                continue
            for section in PAGE_SECTIONS:
                prefix = f"{b.path.rstrip('/')}/{section}/"
                if path.startswith(prefix):  # normpath already dropped a bare trailing /
                    return True
        return False

    def fetch_page(self, url: str) -> tuple[str, str]:
        if not self.allowed(url):
            self.record("tool.fetch_page.refused", {"url": url})
            raise PageRefused(f"page fetching is limited to registered merchant pages: {url!r}")
        resp = self.http.get(url)
        if resp.status_code != 200:
            raise PageRefused(f"{url!r} returned HTTP {resp.status_code}")
        raw = resp.content
        digest = hashlib.sha256(raw).hexdigest()
        if self.put_blob is not None:
            self.put_blob(raw, resp.headers.get("content-type", "text/html"))
        self.record("tool.page_snapshot", {"url": url, "content_hash": digest, "bytes": len(raw)})
        return html_to_text(raw.decode("utf-8", errors="replace")), digest

    # ---- Q-LLM ---------------------------------------------------------------------

    def extract(self, schema: str, text: str) -> Any:
        return roles.extract(self.router, schema, text, seed=self.seed)

    def rank(self, candidates: list[dict[str, Any]], evidence: Any) -> list[int]:
        return roles.rank(self.router, candidates, evidence, seed=self.seed)
