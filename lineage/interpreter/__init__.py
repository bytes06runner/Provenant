"""Plan interpreter: executes a validated plan with provenance labels (CLAUDE.md section 4.5).

Every value is Labeled. Tools that touch the outside world are injected (`Toolbox`); the
deterministic ones (search_manifest, precheck, select_best) are implemented here.

Label rules enforced at run time:
  * Tool outputs are labeled by what they are, never by what they claim: pages and anything an
    LLM extracts or ranks are UNTRUSTED; verified manifests are MERCHANT_SIGNED; planner
    constants are DERIVED (they can never satisfy a USER or MERCHANT_SIGNED contract).
  * Control flow: an `if` whose condition is UNTRUSTED runs its branch with a tainted program
    counter. Values bound there become UNTRUSTED, and the only checkout that can be proposed is
    a Candidate that already passed the contract precheck.
  * Candidates exist only via `precheck`, which builds the checkout from the signed manifest, the
    verified mandate and the vault, and runs the full contract check. So untrusted content can
    influence *which* compliant candidate is picked (the residual risk Blackbox handles), never
    what an authority-bearing field contains.
  * `propose_checkout` re-runs the contract check on the chosen candidate before returning it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from lineage.contracts import ContractResult, ProposedCheckout, check_checkout, compute_total
from lineage.dsl import Plan
from lineage.labels import Label, Labeled, Source, derive, join, untrusted
from lineage.mandate import VerifiedMandate
from lineage.manifest import ManifestError, VerifiedManifest
from lineage.vault import AddressVault

Recorder = Callable[[str, dict[str, Any]], None]

# Tools that bring new candidates into existence. Refused under untrusted control flow.
CANDIDATE_TOOLS = frozenset({"fetch_manifest", "search_manifest", "precheck"})


class InterpreterError(Exception):
    pass


class Toolbox(Protocol):
    """Side-effecting tools. Implementations fetch, verify and call the Q-LLM; the interpreter
    labels whatever they return."""

    def list_merchants(self) -> list[str]: ...

    def fetch_manifest(self, merchant_id: str) -> VerifiedManifest: ...

    def fetch_page(self, url: str) -> tuple[str, str]:
        """(text, content_hash). The interpreter labels it UNTRUSTED."""

    def extract(self, schema: str, text: str) -> Any:
        """Q-LLM: schema-validated JSON from untrusted text. No tools, no mandate."""

    def rank(self, candidates: list[dict[str, Any]], evidence: Any) -> list[int]:
        """Order candidate indices by fit with the evidence. Output is UNTRUSTED."""


@dataclass(frozen=True)
class Candidate:
    """A checkout that passed the full contract check. Its fields keep their own labels."""

    checkout: ProposedCheckout
    manifest: VerifiedManifest
    precheck: ContractResult

    def summary(self) -> dict[str, Any]:
        c = self.checkout
        return {
            "merchant_id": self.manifest.merchant_id,
            "sku": c.sku.value,
            "unit_price": str(c.unit_price.value),
            "total": str(c.amount_total.value),
            "attributes": dict(self.manifest.product(c.sku.value).attributes),
        }


@dataclass(frozen=True)
class Proposal:
    candidate: Candidate
    result: ContractResult
    decision_label: Label  # UNTRUSTED if untrusted content influenced which candidate won
    decision_sources: tuple[str, ...]


@dataclass
class Limits:
    max_steps: int = 500
    max_loop_items: int = 50


@dataclass
class _Frame:
    vars: dict[str, Labeled[Any]] = field(default_factory=dict)


class _Done(Exception):
    def __init__(self, proposal: Proposal) -> None:
        self.proposal = proposal


class Interpreter:
    def __init__(
        self,
        *,
        toolbox: Toolbox,
        mandate: VerifiedMandate,
        vault: AddressVault,
        now: datetime,
        record: Recorder | None = None,
        limits: Limits | None = None,
    ) -> None:
        self.tools = toolbox
        self.mandate = mandate
        self.vault = vault
        self.now = now
        self.record = record or (lambda _t, _p: None)
        self.limits = limits or Limits()
        self.steps = 0
        self.plan_ref = ""

    # ---- entry -------------------------------------------------------------------

    def run(self, plan: Plan) -> Proposal:
        self.plan_ref = f"plan:{plan.plan_hash}"
        self.record("plan.started", {"plan_hash": plan.plan_hash})
        try:
            self._block(plan.body, [_Frame()], tainted=False)
        except _Done as done:
            p = done.proposal
            self.record(
                "plan.proposed",
                {
                    "candidate": p.candidate.summary(),
                    "decision_label": p.decision_label.name,
                    "decision_sources": list(p.decision_sources),
                    "allowed": p.result.allowed,
                },
            )
            return p
        raise InterpreterError("plan finished without proposing a checkout")

    # ---- statements -----------------------------------------------------------------

    def _tick(self) -> None:
        self.steps += 1
        if self.steps > self.limits.max_steps:
            raise InterpreterError(f"plan exceeded {self.limits.max_steps} steps")

    def _block(self, block: list[dict[str, Any]], env: list[_Frame], tainted: bool) -> _Frame:
        """Run a block in a new scope. Returns that scope so for_each can collect from it."""
        frame = _Frame()
        env = [*env, frame]
        for stmt in block:
            self._tick()
            op = stmt["op"]
            if op == "let":
                value = self._expr(stmt["expr"], env, tainted)
                frame.vars[stmt["name"]] = self._taint(value, tainted)
            elif op == "if":
                cond = self._expr(stmt["cond"], env, tainted)
                if not isinstance(cond.value, bool):
                    raise InterpreterError("if condition must be a boolean")
                branch_tainted = tainted or cond.label is Label.UNTRUSTED
                chosen = stmt["then"] if cond.value else stmt.get("else", [])
                self.record(
                    "plan.branch",
                    {"taken": "then" if cond.value else "else", "cond_label": cond.label.name},
                )
                self._block(chosen, env, branch_tainted)
            elif op == "for_each":
                self._for_each(stmt, env, frame, tainted)
            else:
                self._propose(self._expr(stmt["selection"], env, tainted), tainted)
        return frame

    def _for_each(
        self, stmt: dict[str, Any], env: list[_Frame], frame: _Frame, tainted: bool
    ) -> None:
        items = self._expr(stmt["in"], env, tainted)
        if not isinstance(items.value, list):
            raise InterpreterError("for_each needs a list")
        if len(items.value) > self.limits.max_loop_items:
            raise InterpreterError(f"for_each over more than {self.limits.max_loop_items} items")
        collected: list[Labeled[Any]] = []
        loop_tainted = tainted or items.label is Label.UNTRUSTED
        for item in items.value:
            body_frame = _Frame({stmt["var"]: item})
            inner = [*env, body_frame]
            body_scope = self._block(stmt["body"], inner, loop_tainted)
            collect = stmt.get("collect")
            if collect is not None:
                # collect sees the loop variable and the names bound in the body.
                value = self._expr(collect["expr"], [*inner, body_scope], loop_tainted)
                if collect.get("flatten") and isinstance(value.value, list):
                    collected.extend(self._taint(v, loop_tainted) for v in value.value)
                elif value.value is not None:
                    collected.append(self._taint(value, loop_tainted))
        if "collect" in stmt:
            frame.vars[stmt["collect"]["into"]] = self._list(collected, f"collect:{stmt['var']}")

    def _propose(self, selection: Labeled[Any], tainted: bool) -> None:
        if not isinstance(selection.value, Candidate):
            raise InterpreterError(
                "propose_checkout needs a prechecked candidate"
                + (" (decision depends on untrusted data)" if tainted else "")
            )
        cand = selection.value
        final = check_checkout(
            cand.checkout,
            mandate=self.mandate,
            manifest=cand.manifest,
            vault=self.vault,
            now=self.now,
        )
        label = join(selection.label, Label.UNTRUSTED) if tainted else selection.label
        raise _Done(Proposal(cand, final, label, tuple(selection.provenance())))

    # ---- expressions ---------------------------------------------------------------

    def _expr(self, expr: dict[str, Any], env: list[_Frame], tainted: bool) -> Labeled[Any]:
        if "var" in expr:
            for frame in reversed(env):
                if expr["var"] in frame.vars:
                    return frame.vars[expr["var"]]
            raise InterpreterError(f"undefined variable {expr['var']!r}")
        if "const" in expr:
            return Labeled(
                expr["const"], Label.DERIVED, frozenset({Source(Label.DERIVED, self.plan_ref)})
            )
        if "select_best" in expr:
            return self._select_best(expr["select_best"], env, tainted)
        tool = expr["call"]
        if tainted and tool in CANDIDATE_TOOLS:
            # Untrusted control flow may only choose among candidates prechecked before it.
            raise InterpreterError(
                f"{tool} is not allowed under control flow that depends on untrusted data"
            )
        args = {k: self._expr(v, env, tainted) for k, v in expr["args"].items()}
        return self._call(tool, args)

    def _call(self, tool: str, args: dict[str, Labeled[Any]]) -> Labeled[Any]:
        if tool == "list_merchants":
            ids = self.tools.list_merchants()
            src = frozenset({Source(Label.DERIVED, "registry:merchants")})
            out = self._list([Labeled(m, Label.DERIVED, src) for m in ids], "registry:merchants")
        elif tool == "fetch_manifest":
            mid = self._text(args["merchant"], "merchant")
            try:
                out = self.tools.fetch_manifest(mid).labeled()
            except ManifestError as e:
                self.record("tool.fetch_manifest.rejected", {"merchant": mid, "error": str(e)})
                out = Labeled(
                    None, Label.DERIVED, frozenset({Source(Label.DERIVED, "manifest:none")})
                )
        elif tool == "search_manifest":
            out = self._search(self._manifest(args["manifest"]))
        elif tool == "fetch_page":
            url = self._text(args["url"], "url")
            text, digest = self.tools.fetch_page(url)
            out = untrusted(text, f"page:{url}", digest=digest)
        elif tool == "extract":
            schema = self._text(args["schema"], "schema")
            data = self.tools.extract(schema, str(args["text"].value))
            out = self._untrusted_from(data, args["text"], f"qllm:extract:{schema}")
        elif tool == "rank":
            cands = self._candidates(args["candidates"])
            order = self.tools.rank([c.summary() for c in cands], args["evidence"].value)
            out = self._untrusted_from(order, args["evidence"], "qllm:rank")
        else:  # precheck
            out = self._precheck(self._manifest(args["manifest"]), args["sku"])
        self.record(
            f"tool.{tool}",
            {"label": out.label.name, "sources": sorted(out.provenance())[:20]},
        )
        return out

    # ---- deterministic tools ----------------------------------------------------------

    def _search(self, manifest: VerifiedManifest) -> Labeled[Any]:
        """Signed skus that are in stock and meet the mandate's attribute rules."""
        m = self.mandate.mandate
        hits = []
        for p in manifest.manifest.catalog:
            if p.stock < 1:
                continue
            if any(p.attributes.get(k) != v for k, v in m.required_attributes.items()):
                continue
            if any(p.attributes.get(k) in banned for k, banned in m.forbidden_attributes.items()):
                continue
            hits.append(manifest.sku(p.sku))
        return self._list(hits, manifest.ref)

    def _precheck(self, manifest: VerifiedManifest, sku: Labeled[Any]) -> Labeled[Any]:
        """Build the checkout from trusted sources only, and run every contract on it."""
        if sku.label is not Label.MERCHANT_SIGNED or not isinstance(sku.value, str):
            return self._none("precheck:refused-unsigned-sku", sku)
        try:
            signed_sku = manifest.sku(sku.value)
        except ManifestError:
            return self._none("precheck:not-in-catalog", sku)
        checkout = ProposedCheckout(
            payee=manifest.payee(),
            sku=signed_sku,
            unit_price=manifest.unit_price(sku.value),
            quantity=self.mandate.quantity(),
            amount_total=derive(
                compute_total,
                manifest.unit_price(sku.value),
                self.mandate.quantity(),
                manifest.shipping_flat(),
                manifest.tax_rate(),
            ),
            shipping_address=self.vault.lookup(
                self.mandate.mandate.user_id, self.mandate.mandate.ship_to_ref
            ),
            currency=self.mandate.currency(),
        )
        result = check_checkout(
            checkout, mandate=self.mandate, manifest=manifest, vault=self.vault, now=self.now
        )
        self.record(
            "contract.precheck",
            {
                "merchant_id": manifest.merchant_id,
                "sku": sku.value,
                "allowed": result.allowed,
                "violations": [
                    {"field": v.field.value, "rule": v.rule.value, "detail": v.detail}
                    for v in result.violations
                ],
            },
        )
        if not result.allowed:
            return self._none("precheck:blocked", sku)
        cand = Candidate(checkout, manifest, result)
        fields = list(checkout.fields().values())
        return Labeled(cand, Label.DERIVED, frozenset().union(*(f.sources for f in fields)))

    def _select_best(self, spec: dict[str, Any], env: list[_Frame], tainted: bool) -> Labeled[Any]:
        """Pick a prechecked candidate. The ranking may be untrusted; the choice set is not."""
        pool = self._expr(spec["from"], env, tainted)
        cands = self._candidates(pool)
        if not cands:
            raise InterpreterError("select_best has no prechecked candidates to choose from")
        ranking = self._expr(spec["ranking"], env, tainted)
        order = ranking.value if isinstance(ranking.value, list) else []
        valid = [i for i in order if isinstance(i, int) and not isinstance(i, bool)]
        valid = [i for i in valid if 0 <= i < len(cands)]
        index = valid[0] if valid else 0  # malformed ranking: deterministic default
        chosen = pool.value[index]
        self.record(
            "plan.select_best",
            {
                "index": index,
                "ranking_label": ranking.label.name,
                "ranking_valid": bool(valid),
                "options": len(cands),
            },
        )
        # The wrapper records how the choice was made; the candidate's fields are untouched.
        return Labeled(
            chosen.value,
            join(chosen.label, ranking.label),
            chosen.sources | ranking.sources,
        )

    # ---- helpers ---------------------------------------------------------------

    def _list(self, items: list[Labeled[Any]], ref: str) -> Labeled[Any]:
        if not items:
            return Labeled([], Label.DERIVED, frozenset({Source(Label.DERIVED, ref)}))
        label = join(Label.DERIVED, *(i.label for i in items))
        return Labeled(items, label, frozenset().union(*(i.sources for i in items)))

    def _none(self, why: str, cause: Labeled[Any]) -> Labeled[Any]:
        label = join(Label.DERIVED, cause.label)
        return Labeled(None, label, cause.sources | {Source(Label.DERIVED, why)})

    @staticmethod
    def _taint(value: Labeled[Any], tainted: bool) -> Labeled[Any]:
        if not tainted or value.label is Label.UNTRUSTED:
            return value
        return Labeled(
            value.value,
            Label.UNTRUSTED,
            value.sources | {Source(Label.UNTRUSTED, "control-flow:untrusted-branch")},
        )

    @staticmethod
    def _untrusted_from(value: Any, origin: Labeled[Any], ref: str) -> Labeled[Any]:
        return Labeled(value, Label.UNTRUSTED, origin.sources | {Source(Label.UNTRUSTED, ref)})

    @staticmethod
    def _text(value: Labeled[Any], name: str) -> str:
        if not isinstance(value.value, str):
            raise InterpreterError(f"{name} must be a string")
        return value.value

    @staticmethod
    def _manifest(value: Labeled[Any]) -> VerifiedManifest:
        if (
            not isinstance(value.value, VerifiedManifest)
            or value.label is not Label.MERCHANT_SIGNED
        ):
            raise InterpreterError("expected a verified manifest")
        return value.value

    @staticmethod
    def _candidates(value: Labeled[Any]) -> list[Candidate]:
        if not isinstance(value.value, list):
            raise InterpreterError("expected a list of candidates")
        out = []
        for item in value.value:
            if not isinstance(item, Labeled) or not isinstance(item.value, Candidate):
                raise InterpreterError("candidates must come from precheck")
            out.append(item.value)
        return out
