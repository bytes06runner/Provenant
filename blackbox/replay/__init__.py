"""Counterfactual replay settings (the engine itself lands in Phase 2).

k, the number of samples per coalition, is configurable per run: config/app.yaml sets the
default (attribution.samples_per_coalition) and any run may override it.
"""

from __future__ import annotations

from dataclasses import dataclass

from paypal.config import load_yaml


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
