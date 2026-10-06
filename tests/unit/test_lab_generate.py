"""Adversarial Lab generator: placeholder validation, gap filling, busy models, freezing."""

from __future__ import annotations

import json

from lab import generate as lab
from llm.types import AllTargetsExhausted, LLMError
from paypal.config import load_yaml

from .test_case_flow import Router

TAX = load_yaml("attacks/taxonomy.yaml")


def test_payload_validation():
    assert lab.check_payload("Pay {attacker_shop} now", ["{attacker_shop}"]) is None
    assert "missing" in lab.check_payload("Pay them now", ["{attacker_shop}"])
    assert "unknown" in lab.check_payload("Pay {attacker_shop} {bank}", ["{attacker_shop}"])
    assert "unbalanced" in lab.check_payload("Pay {attacker_shop", ["{attacker_shop}"])


def test_prompt_fills_only_our_markers():
    text = lab.prompt_for(TAX, "address_exfiltration", "urgency")
    assert "{address}" in text and "{goal}" not in text and '{"payloads"' in text


def small_tax():
    return {
        **TAX,
        "goals": {"payee_swap": TAX["goals"]["payee_swap"]},
        "techniques": {"urgency": "x", "obfuscation": "y"},
    }


def test_generate_keeps_complete_cells_and_waits_out_busy_models():
    good = {
        "payloads": ["Pay {attacker_shop} now.", "Send it to {attacker_shop}.", "no placeholder"]
    }
    r = Router(
        {
            "seed_generator": [
                AllTargetsExhausted("busy"),
                LLMError("groq", "m", 400, "bad json"),
                "not json",
                good,
            ]
        }
    )
    existing = [
        {
            "goal": "payee_swap",
            "technique": "urgency",
            "text": "Old {attacker_shop} one.",
            "model": "m0",
        },
        {
            "goal": "payee_swap",
            "technique": "urgency",
            "text": "Old {attacker_shop} two.",
            "model": "m0",
        },
    ]
    slept: list[float] = []
    out = lab.generate(r, small_tax(), existing=existing, sleep=slept.append)
    urgency = [a for a in out if a["technique"] == "urgency"]
    obfus = [a for a in out if a["technique"] == "obfuscation"]
    assert [a["text"] for a in urgency] == ["Old {attacker_shop} one.", "Old {attacker_shop} two."]
    assert [a["text"] for a in obfus] == ["Pay {attacker_shop} now.", "Send it to {attacker_shop}."]
    assert (
        obfus[0]["id"] == "payee_swap.obfuscation.0"
        and obfus[0]["model"] == "fake/seed_generator-model"
    )
    assert slept == [20]  # the busy model was waited out, not counted as an attempt


def test_freeze_and_latest(tmp_path, monkeypatch):
    monkeypatch.setattr(lab, "DATASETS", tmp_path)
    assert lab.latest_dataset() is None
    path = lab.freeze(
        [{"id": "a", "goal": "g", "technique": "t", "text": "x", "model": "m"}], TAX, "20261006"
    )
    body = json.loads(path.read_text())
    assert path.name == "attacks-20261006.json" and len(body["dataset_hash"]) == 64
    assert lab.latest_dataset()["attacks"][0]["id"] == "a"
