"""The ruling's text (CLAUDE.md section 4.7, step D).

The narrator model receives only the computed result, never raw evidence, and may not change a
number. A post-check extracts every number from its text; if any is not present in the computed
result, the text is rejected and a deterministic ruling is used instead. Both outcomes are
recorded.
"""

from __future__ import annotations

import json
import re
from typing import Any

from llm import roles
from llm.types import LLMRequest, Message

# A number may end a sentence ("Refund 45.26."): only a following digit or letter extends it.
_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?!\w|\.\d)")


def allowed_numbers(result: dict[str, Any]) -> set[str]:
    nums = set(_NUMBER.findall(json.dumps(result)))
    out = set(nums)
    for n in nums:  # 50.00 may be written as 50; 0.5000 as 0.5
        out.add(n.rstrip("0").rstrip(".") if "." in n else n)
    return out


def check_numbers(text: str, result: dict[str, Any]) -> list[str]:
    allowed = allowed_numbers(result)
    return [
        n
        for n in _NUMBER.findall(text)
        if n not in allowed and n.rstrip("0").rstrip(".") not in allowed
    ]


def deterministic_ruling(result: dict[str, Any]) -> dict[str, Any]:
    shares = result.get("shares_percent") or {}
    findings = [f"Order {result['order_id']} was examined under case {result['case_id']}."]
    findings += [f"Fact: {f}" for f in result.get("fact_lines", [])]
    if shares:
        findings.append(
            "Fault shares: " + ", ".join(f"{k} {v} percent" for k, v in shares.items()) + "."
        )
    return {
        "heading": f"Ruling on order {result['order_id']}",
        "findings": findings,
        "holding": result.get("holding", "See the findings."),
        "remedy": result.get("remedy_line", "No money moves."),
    }


def narrate(router: roles.Router, result: dict[str, Any]) -> dict[str, Any]:
    req = roles._request(  # noqa: SLF001  (same request builder as the other roles)
        router,
        "narrator",
        roles.prompt("narrator_ruling_v1"),
        json.dumps(result, indent=1),
        "narrator_ruling_v1",
    )
    resp = router.call(
        "narrator",
        LLMRequest(
            messages=(req.messages[0], Message("user", req.messages[1].content)),
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            json_schema=req.json_schema,
            schema_name=req.schema_name,
            structured=req.structured,
        ),
    )
    try:
        ruling = roles.parse_json(resp.text)
        text = " ".join(
            [ruling["heading"], *ruling["findings"], ruling["holding"], ruling["remedy"]]
        )
    except (ValueError, KeyError, TypeError):
        return {
            "ruling": deterministic_ruling(result),
            "source": "deterministic",
            "rejected": "narrator output was not valid",
        }
    bad = check_numbers(text, result)
    if bad or "—" in text:
        return {
            "ruling": deterministic_ruling(result),
            "source": "deterministic",
            "rejected": f"numbers not in the computed result: {bad}" if bad else "em dash",
        }
    return {"ruling": ruling, "source": f"{resp.provider}/{resp.model}"}
