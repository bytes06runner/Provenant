"""Causal fault attribution: exact Shapley values over three players, with bootstrap CIs.

Players (CLAUDE.md section 4.7):  U = the user's original wording,  M = merchant content,
A = the agent's policy. For each coalition S of players we replay the purchase with every player
in S replaced by its corrected reference (do(S)) and estimate

    v(S) = P(bad | do(S))           from k replayed samples (sample mean)

Fixing players removes badness, so the game we attribute is the badness removed,
w(S) = v({}) - v(S). With three players there are 8 coalitions, so the Shapley value is exact:

    phi_i = sum over S not containing i of  |S|! (n - |S| - 1)! / n!  *  (v(S) - v(S + i))

Fault shares are the positive Shapley values normalized to sum to 1.

Uncertainty (decided 2026-10-06, replacing a bootstrap): each coalition's v(S) gets a Jeffreys
posterior, Beta(x + 1/2, k - x + 1/2) for x bad outcomes in k samples. Seeded draws from the
eight posteriors are pushed through the exact Shapley map, and the percentiles of the resulting
shares are the reported intervals. Unlike a bootstrap, 4 bad out of 4 does not collapse to a
zero-width interval: with k = 4 the data cannot rule out a small chance of a good purchase.
Coalitions are treated as independent, which is conservative when replays share plans (common
random numbers pair them). Per-coalition Jeffreys intervals for v(S) are reported too. If any
share interval is wider than the configured threshold, or nothing can be attributed, the case
escalates to human review instead of an automatic remedy.

Point estimates are exact (fractions); the interval draws use seeded floating point. No LLM
ever computes these numbers.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import combinations
from math import factorial

PLAYERS: tuple[str, ...] = ("U", "M", "A")


class AttributionError(ValueError):
    pass


Coalition = frozenset[str]


def coalitions(players: Sequence[str] = PLAYERS) -> list[Coalition]:
    return [frozenset(c) for r in range(len(players) + 1) for c in combinations(players, r)]


def label(c: Coalition) -> str:
    return "do(" + ",".join(p for p in PLAYERS if p in c) + ")" if c else "observed"


def shapley(
    v: Mapping[Coalition, Fraction], players: Sequence[str] = PLAYERS
) -> dict[str, Fraction]:
    """Exact Shapley value of each player for removing badness: w(S) = v({}) - v(S)."""
    n = len(players)
    missing = [label(c) for c in coalitions(players) if c not in v]
    if missing:
        raise AttributionError(f"missing coalitions: {missing}")
    phi: dict[str, Fraction] = {}
    for i in players:
        total = Fraction(0)
        others = [p for p in players if p != i]
        for r in range(n):
            for s in combinations(others, r):
                S = frozenset(s)
                weight = Fraction(factorial(r) * factorial(n - r - 1), factorial(n))
                total += weight * (v[S] - v[S | {i}])
        phi[i] = total
    return phi


def shares(phi: Mapping[str, Fraction]) -> dict[str, Fraction] | None:
    """Positive contributions normalized to 1, or None if nothing is attributable."""
    pos = {p: max(x, Fraction(0)) for p, x in phi.items()}
    total = sum(pos.values(), Fraction(0))
    if total == 0:
        return None
    return {p: x / total for p, x in pos.items()}


def means(samples: Mapping[Coalition, Sequence[int]]) -> dict[Coalition, Fraction]:
    out = {}
    for c, xs in samples.items():
        if not xs:
            raise AttributionError(f"no samples for {label(c)}")
        if any(x not in (0, 1) for x in xs):
            raise AttributionError(f"outcomes for {label(c)} must be 0 or 1")
        out[c] = Fraction(sum(xs), len(xs))
    return out


@dataclass(frozen=True)
class Attribution:
    k: int
    v: dict[Coalition, Fraction]
    phi: dict[str, Fraction]
    shares: dict[str, Fraction] | None
    ci: dict[str, tuple[Fraction, Fraction]]
    ci_level: Fraction
    escalate: bool
    reason: str
    v_ci: dict[Coalition, tuple[Fraction, Fraction]] = field(default_factory=dict)
    interval: str = "jeffreys"

    def majority(self) -> str | None:
        if self.shares is None:
            return None
        return max(PLAYERS, key=lambda p: (self.shares[p], -PLAYERS.index(p)))  # type: ignore[index]

    def leaders(self) -> list[str]:
        """Every party holding the largest share (more than one on a tie)."""
        if self.shares is None:
            return []
        top = max(self.shares.values())
        return [p for p in PLAYERS if top > 0 and self.shares[p] == top]

    def to_dict(self) -> dict[str, object]:
        """JSON-safe (decimal strings), for the Flight Recorder and the narrator."""

        def d(x: Fraction) -> str:
            return f"{float(x):.4f}"

        return {
            "k": self.k,
            "v": {label(c): d(x) for c, x in sorted(self.v.items(), key=lambda kv: len(kv[0]))},
            "phi": {p: d(x) for p, x in self.phi.items()},
            "shares": {p: d(x) for p, x in self.shares.items()} if self.shares else None,
            "ci": {p: [d(lo), d(hi)] for p, (lo, hi) in self.ci.items()},
            "ci_level": d(self.ci_level),
            "interval": self.interval,
            "v_ci": {
                label(c): [d(lo), d(hi)]
                for c, (lo, hi) in sorted(self.v_ci.items(), key=lambda kv: len(kv[0]))
            },
            "escalate": self.escalate,
            "reason": self.reason,
            "majority": self.majority(),
        }


def _shares_float(v: Mapping[Coalition, float]) -> dict[str, float] | None:
    n = len(PLAYERS)
    phi = {}
    for i in PLAYERS:
        others = [p for p in PLAYERS if p != i]
        total = 0.0
        for r in range(n):
            w = factorial(r) * factorial(n - r - 1) / factorial(n)
            for s in combinations(others, r):
                S = frozenset(s)
                total += w * (v[S] - v[S | {i}])
        phi[i] = max(total, 0.0)
    z = sum(phi.values())
    return {p: x / z for p, x in phi.items()} if z > 1e-12 else None


def _q(sorted_xs: Sequence[float], q: Fraction) -> Fraction:
    idx = min(len(sorted_xs) - 1, max(0, int(q * len(sorted_xs))))
    return Fraction(sorted_xs[idx]).limit_denominator(10_000)


def attribute(
    samples: Mapping[Coalition, Sequence[int]],
    *,
    draws: int,
    ci_level: Fraction,
    max_ci_width: Fraction,
    seed: str,
) -> Attribution:
    """Shapley shares with Jeffreys posterior intervals propagated through the Shapley map.
    `seed` makes the draws reproducible."""
    v = means(samples)
    phi = shapley(v)
    sh = shares(phi)
    k = min(len(xs) for xs in samples.values())
    alpha = (1 - ci_level) / 2
    rng = random.Random(int(hashlib.sha256(seed.encode()).hexdigest()[:16], 16))  # noqa: S311
    counts = {c: (sum(xs), len(xs)) for c, xs in samples.items()}
    v_draws: dict[Coalition, list[float]] = {c: [] for c in samples}
    s_draws: dict[str, list[float]] = {p: [] for p in PLAYERS}
    for _ in range(draws):
        vd = {c: rng.betavariate(x + 0.5, n - x + 0.5) for c, (x, n) in counts.items()}
        for c, x in vd.items():
            v_draws[c].append(x)
        sd = _shares_float(vd)
        for p in PLAYERS:
            s_draws[p].append(sd[p] if sd else 0.0)
    v_ci = {}
    for c, xs in v_draws.items():
        xs.sort()
        v_ci[c] = (_q(xs, alpha), _q(xs, 1 - alpha))
    ci = {}
    for p in PLAYERS:
        xs = sorted(s_draws[p])
        ci[p] = (_q(xs, alpha), _q(xs, 1 - alpha))
    if sh is None:
        return Attribution(
            k,
            v,
            phi,
            None,
            ci,
            ci_level,
            True,
            "no player's correction reduces the chance of a bad purchase",
            v_ci,
        )
    widest = max(hi - lo for lo, hi in ci.values())
    escalate = widest > max_ci_width
    reason = (
        f"widest {float(ci_level):.0%} interval is {float(widest):.2f}, above the "
        f"{float(max_ci_width):.2f} limit for automatic remedies"
        if escalate
        else "attribution is precise enough for an automatic remedy proposal"
    )
    return Attribution(k, v, phi, sh, ci, ci_level, escalate, reason, v_ci)
