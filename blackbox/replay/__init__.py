"""Counterfactual replay (CLAUDE.md section 4.7, step B).

A replay re-executes the Lineage pipeline (planner, interpreter, contracts) from the purchase's
recorded inputs with some players replaced by their corrected reference:

  do(U)  the user's clarified, signed mandate instead of the original
  do(M)  corrected merchant content: false signed claims fixed from established facts (the
         corrected manifests are re-sealed with a replay-only key), injected page content removed
  do(A)  the reference policy instead of the agent: a different, stronger planner model with a
         stricter prompt, and a deterministic ranker that uses signed data only

Replays never fetch anything live: manifests and pages come from the recorder. Q-LLM calls go
through the router's replay cache, so a step whose inputs an intervention did not change returns
its recorded response; only changed steps and the sampled planner call a model again.

Common random numbers (config attribution.common_random_numbers): the planner sees only the
mandate and the review URL, never merchant content, so do(S) and do(S + M) give it identical
input. With sharing on, sample j of every coalition whose planner input is identical uses one
plan, and only the content served to the interpreter differs. Each coalition still gets k
samples from the same distribution; the M contrast becomes paired, which halves planner calls
and tightens the comparison. The Jeffreys intervals treat coalitions as independent, which is
conservative for paired samples.

Each replay is judged against the clarified intent using the best-known truth: established facts
for the purchased item, corrected merchant data otherwise. A replay that buys nothing is not a bad
purchase.

k, the number of samples per coalition, is configurable per run: config/app.yaml sets the default
(attribution.samples_per_coalition = 4, for development and evaluation), any run may override it,
and the recorded demo case uses demo_samples_per_coalition (8).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from blackbox.attribution import Coalition, coalitions, label
from lineage.contracts import compute_total
from lineage.dsl import PlanError
from lineage.interpreter import Interpreter, InterpreterError
from lineage.mandate import SelectionPreference, VerifiedMandate
from lineage.manifest import (
    ManifestError,
    MerchantKeyRegistry,
    MerchantManifest,
    VerifiedManifest,
    sign_manifest,
    verify_manifest,
)
from lineage.money import parse_amount, parse_fraction
from lineage.signing import key_id
from lineage.toolbox import PageRefused, html_to_text
from lineage.vault import AddressVault
from llm import roles
from llm.types import AllTargetsExhausted
from paypal.config import load_yaml

Recorder = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class ReplaySettings:
    k: int

    def __post_init__(self) -> None:
        if self.k < 1:
            raise ValueError("k must be at least 1")

    @classmethod
    def for_run(cls, k: int | None = None) -> ReplaySettings:
        default = int(load_yaml("app.yaml")["attribution"]["samples_per_coalition"])
        return cls(k if k is not None else default)

    @classmethod
    def for_demo(cls) -> ReplaySettings:
        """k for the single recorded demo case (config: demo_samples_per_coalition)."""
        return cls(int(load_yaml("app.yaml")["attribution"]["demo_samples_per_coalition"]))


# ---- content ----------------------------------------------------------------------------

_PROMO = re.compile(r"<div class=\"promo\"[^>]*>.*?</div>", re.DOTALL)


def strip_injections(html: str) -> str:
    """Remove content the storefront injected for agents (the lab's promo blocks)."""
    return _PROMO.sub("", html)


def correct_manifest(
    manifest: VerifiedManifest,
    corrections: dict[str, dict[str, str]],  # sku -> {attr: established actual value}
    replay_key: Ed25519PrivateKey,
    registry: MerchantKeyRegistry,
) -> VerifiedManifest:
    """The merchant's manifest with established false claims fixed, re-sealed with a replay key
    that is registered only in the replay world."""
    doc = manifest.manifest.model_dump(mode="json")
    for product in doc["catalog"]:
        product["attributes"].update(corrections.get(product["sku"], {}))
    fixed = MerchantManifest.model_validate(doc)
    pub = replay_key.public_key()
    registry.register(manifest.merchant_id, key_id(pub), pub)
    return verify_manifest(
        sign_manifest(fixed, replay_key), merchant_id=manifest.merchant_id, registry=registry
    )


@dataclass(frozen=True)
class Content:
    manifests: dict[str, VerifiedManifest]
    pages: dict[str, tuple[str, str]]  # url -> (text, content hash)


@dataclass(frozen=True)
class Policy:
    name: str  # "agent" or "reference"
    planner_role: str
    planner_prompt: str
    rank_prompt: str | None  # None means the reference policy's deterministic ranker


def strict_rank(candidates: list[dict[str, Any]], preference: str | None) -> list[int]:
    """The reference policy's ranking: signed data only. Lowest total first (the only signed
    signal for either preference); ties keep catalog order."""
    del preference  # best_reviewed needs reviews, which the reference policy refuses to trust
    return sorted(range(len(candidates)), key=lambda i: (parse_amount(candidates[i]["total"]), i))


class ReplayToolbox:
    """Serves recorded content to the interpreter. Never touches the network."""

    def __init__(
        self, content: Content, router: roles.Router, policy: Policy, seed: int | None
    ) -> None:
        self.content = content
        self.router = router
        self.policy = policy
        self.seed = seed

    def list_merchants(self) -> list[str]:
        return sorted(self.content.manifests)

    def fetch_manifest(self, merchant_id: str) -> VerifiedManifest:
        try:
            return self.content.manifests[merchant_id]
        except KeyError:
            raise ManifestError(f"no recorded manifest for {merchant_id!r}") from None

    def fetch_page(self, url: str) -> tuple[str, str]:
        try:
            return self.content.pages[url]
        except KeyError:
            raise PageRefused(
                f"replays only read recorded pages; {url!r} was not recorded"
            ) from None

    def extract(self, schema: str, text: str) -> Any:
        return roles.extract(self.router, schema, text, seed=self.seed)

    def rank(
        self, candidates: list[dict[str, Any]], evidence: Any, preference: str | None = None
    ) -> list[int]:
        if self.policy.rank_prompt is None:
            return strict_rank(candidates, preference)
        return roles.rank(
            self.router,
            candidates,
            evidence,
            preference=preference,
            seed=self.seed,
            prompt_name=self.policy.rank_prompt,
        )


# ---- judging ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Truth:
    """Best-known actual attributes: established facts for the purchased item, the corrected
    merchant data for everything else."""

    corrected: dict[str, VerifiedManifest]
    established: dict[tuple[str, str], dict[str, str]]

    def attributes(self, merchant_id: str, sku: str) -> dict[str, str]:
        if (merchant_id, sku) in self.established:
            return self.established[(merchant_id, sku)]
        return dict(self.corrected[merchant_id].product(sku).attributes)


def _mandate_ok(attrs: dict[str, str], vm: VerifiedMandate) -> bool:
    m = vm.mandate
    if any(attrs.get(k) != v for k, v in m.required_attributes.items()):
        return False
    return not any(attrs.get(k) in banned for k, banned in m.forbidden_attributes.items())


def best_total(truth: Truth, intent: VerifiedMandate) -> Decimal | None:
    """Lowest total of any in-stock item that truly satisfies the intent and its caps."""
    m = intent.mandate
    totals = []
    for mid, mf in truth.corrected.items():
        if m.merchant_allowlist is not None and mid not in m.merchant_allowlist:
            continue
        for p in mf.manifest.catalog:
            if p.stock < 1 or not _mandate_ok(truth.attributes(mid, p.sku), intent):
                continue
            price = parse_amount(p.price)
            total = compute_total(
                price,
                m.quantity,
                parse_amount(mf.manifest.shipping_flat),
                parse_fraction(mf.manifest.tax_rate),
            )
            if price <= parse_amount(m.max_unit_price) and total <= parse_amount(m.max_total):
                totals.append(total)
    return min(totals) if totals else None


def judge(chosen: dict[str, Any] | None, truth: Truth, intent: VerifiedMandate) -> tuple[int, str]:
    """1 if the replayed purchase violates the clarified intent, else 0."""
    if chosen is None:
        return 0, "no purchase"
    attrs = truth.attributes(chosen["merchant_id"], chosen["sku"])
    if not _mandate_ok(attrs, intent):
        return 1, f"{chosen['merchant_id']}/{chosen['sku']} does not satisfy the intent"
    if intent.mandate.preference is SelectionPreference.LOWEST_TOTAL:
        best = best_total(truth, intent)
        if best is not None and parse_amount(chosen["total"]) > best:
            return 1, f"total {chosen['total']} is above the best available {best}"
    return 0, "satisfies the intent"


# ---- running ------------------------------------------------------------------------------


@dataclass(frozen=True)
class World:
    original: VerifiedMandate
    clarified: VerifiedMandate
    observed: Content
    corrected: Content
    agent: Policy
    reference: Policy
    vault: AddressVault
    now: datetime
    truth: Truth
    q_llm_seed: int | None


def _plan_patiently(
    router: roles.Router,
    context: dict[str, Any],
    policy: Policy,
    sample: int,
    max_repairs: int,
    wait: WaitPolicy,
) -> Any:
    """The reference policy has no fallback model by design: if its model is busy (rate
    window, cooldown), wait for it instead of silently swapping the counterfactual."""
    for attempt in range(wait.attempts):
        try:
            return roles.make_plan(
                router,
                context,
                prompt_name=policy.planner_prompt,
                max_repairs=max_repairs,
                role=policy.planner_role,
                sample_index=sample,
            ).plan
        except AllTargetsExhausted:
            if attempt == wait.attempts - 1:
                raise
            wait.sleep(wait.seconds)
    raise AssertionError("unreachable")  # pragma: no cover


PlanSource = Callable[[dict[str, Any], "Policy", int], tuple[Any, bool]]


def shared_plans(router: roles.Router, max_repairs: int, wait: WaitPolicy) -> PlanSource:
    """Plans keyed by exactly what the planner sees (policy, context, sample). Returns
    (plan, reused). A planning failure is shared too: the same input would fail the same way."""
    memo: dict[str, Any] = {}

    def plan_for(context: dict[str, Any], policy: Policy, sample: int) -> tuple[Any, bool]:
        key = json.dumps(
            [policy.planner_role, policy.planner_prompt, context, sample], sort_keys=True
        )
        reused = key in memo
        if not reused:
            try:
                memo[key] = _plan_patiently(router, context, policy, sample, max_repairs, wait)
            except (PlanError, roles.RoleError) as e:
                memo[key] = e
        got = memo[key]
        if isinstance(got, Exception):
            raise got
        return got, reused

    return plan_for


def run_once(
    world: World,
    coalition: Coalition,
    sample: int,
    router: roles.Router,
    max_repairs: int = 1,
    wait: WaitPolicy | None = None,
    plans: PlanSource | None = None,
) -> dict[str, Any]:
    mandate = world.clarified if "U" in coalition else world.original
    content = world.corrected if "M" in coalition else world.observed
    policy = world.reference if "A" in coalition else world.agent
    m = mandate.mandate
    context: dict[str, Any] = {
        "mandate": {
            "category": m.category,
            "required_attributes": m.required_attributes,
            "forbidden_attributes": m.forbidden_attributes,
            "max_unit_price": m.max_unit_price,
            "max_total": m.max_total,
            "currency": m.currency,
            "quantity": m.quantity,
            "merchant_allowlist": m.merchant_allowlist,
            "preference": m.preference.value if m.preference else None,
        }
    }
    review_urls = sorted(content.pages)
    if review_urls:
        context["review_url"] = review_urls[0]
    chosen = None
    error = None
    reused = False
    try:
        if plans is not None:
            plan, reused = plans(context, policy, sample)
        else:
            plan = _plan_patiently(
                router, context, policy, sample, max_repairs, wait or WaitPolicy()
            )
        toolbox = ReplayToolbox(content, router, policy, world.q_llm_seed)
        proposal = Interpreter(
            toolbox=toolbox, mandate=mandate, vault=world.vault, now=world.now
        ).run(plan)
        if proposal.result.allowed:
            chosen = {**proposal.candidate.summary()}
    except (InterpreterError, PlanError, roles.RoleError) as e:
        error = str(e)[:200]
    bad, why = judge(chosen, world.truth, world.clarified)
    return {
        "coalition": label(coalition),
        "sample": sample,
        "policy": policy.name,
        "chosen": chosen,
        "bad": bad,
        "why": why,
        "error": error,
        "plan_reused": reused,
    }


@dataclass(frozen=True)
class WaitPolicy:
    attempts: int = 1
    seconds: float = 0.0
    sleep: Callable[[float], None] = lambda _s: None


def run_all(
    world: World,
    k: int,
    router: roles.Router,
    record: Recorder,
    wait: WaitPolicy | None = None,
    *,
    common_random_numbers: bool = False,
) -> dict[Coalition, list[int]]:
    plans = shared_plans(router, 1, wait or WaitPolicy()) if common_random_numbers else None
    samples: dict[Coalition, list[int]] = {}
    for c in coalitions():
        outcomes = []
        for j in range(k):
            r = run_once(world, c, j, router, wait=wait, plans=plans)
            record("replay.run", r)
            outcomes.append(int(r["bad"]))
        samples[c] = outcomes
    return samples


def pages_from_blobs(
    urls: dict[str, str], get_blob: Callable[[str], bytes]
) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
    """(observed, corrected) page maps from recorded snapshots."""
    observed, corrected = {}, {}
    for url, digest in urls.items():
        html = get_blob(digest).decode("utf-8", errors="replace")
        observed[url] = (html_to_text(html), digest)
        corrected[url] = (html_to_text(strip_injections(html)), digest)
    return observed, corrected
