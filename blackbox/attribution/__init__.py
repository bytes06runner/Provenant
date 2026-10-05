"""Causal fault attribution: exact Shapley values over three players, with bootstrap CIs.

Players (CLAUDE.md section 4.7):  U = the user's original wording,  M = merchant content,
A = the agent's policy. For each coalition S of players we replay the purchase with every player
in S replaced by its corrected reference (do(S)) and estimate

    v(S) = P(bad | do(S))           from k replayed samples (sample mean)

Fixing players removes badness, so the game we attribute is the badness removed,
w(S) = v({}) - v(S). With three players there are 8 coalitions, so the Shapley value is exact:

    phi_i = sum over S not containing i of  |S|! (n - |S| - 1)! / n!  *  (v(S) - v(S + i))

Fault shares are the positive Shapley values normalized to sum to 1. Uncertainty comes from the
finite samples: bootstrap resamples each coalition's outcomes with replacement and recomputes the
shares, giving percentile confidence intervals. When replays use common random numbers (sample j
of coalitions that give the planner identical input share one plan, see blackbox/replay), the
samples are paired across coalitions and the bootstrap resamples sample indices jointly.
If any interval is wider than the configured threshold, or nothing can be attributed, the case
escalates to human review instead of an automatic remedy.

All arithmetic is exact (fractions). No LLM ever computes these numbers.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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


def _percentile(sorted_xs: Sequence[Fraction], q: Fraction) -> Fraction:
    """Nearest-rank percentile (q in [0, 1])."""
    idx = min(len(sorted_xs) - 1, max(0, int(q * len(sorted_xs))))
    return sorted_xs[idx]


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
            "escalate": self.escalate,
            "reason": self.reason,
            "majority": self.majority(),
        }


def attribute(
    samples: Mapping[Coalition, Sequence[int]],
    *,
    resamples: int,
    ci_level: Fraction,
    max_ci_width: Fraction,
    seed: str,
    paired: bool = False,
) -> Attribution:
    """Shapley shares with bootstrap percentile CIs. `seed` makes the bootstrap reproducible.
    `paired`: sample j is one draw across all coalitions (common random numbers)."""
    v = means(samples)
    phi = shapley(v)
    sh = shares(phi)
    ks = {len(xs) for xs in samples.values()}
    k = min(ks)
    if paired and len(ks) != 1:
        raise AttributionError("paired samples need the same k in every coalition")
    if sh is None:
        zero = {p: (Fraction(0), Fraction(0)) for p in PLAYERS}
        return Attribution(
            k,
            v,
            phi,
            None,
            zero,
            ci_level,
            True,
            "no player's correction reduces the chance of a bad purchase",
        )

    rng = random.Random(int(hashlib.sha256(seed.encode()).hexdigest()[:16], 16))  # noqa: S311
    draws: dict[str, list[Fraction]] = {p: [] for p in PLAYERS}
    for _ in range(resamples):
        if paired:
            idx = [rng.randrange(k) for _ in range(k)]
            boot = {c: [xs[i] for i in idx] for c, xs in samples.items()}
        else:
            boot = {c: [rng.choice(xs) for _ in xs] for c, xs in samples.items()}
        bs = shares(shapley(means(boot)))
        for p in PLAYERS:
            draws[p].append(bs[p] if bs else Fraction(0))
    alpha = (1 - ci_level) / 2
    ci = {}
    for p in PLAYERS:
        xs = sorted(draws[p])
        ci[p] = (_percentile(xs, alpha), _percentile(xs, 1 - alpha))
    widest = max(hi - lo for lo, hi in ci.values())
    escalate = widest > max_ci_width
    reason = (
        f"widest {float(ci_level):.0%} interval is {float(widest):.2f}, above the "
        f"{float(max_ci_width):.2f} limit for automatic remedies"
        if escalate
        else "attribution is precise enough for an automatic remedy proposal"
    )
    return Attribution(k, v, phi, sh, ci, ci_level, escalate, reason)
