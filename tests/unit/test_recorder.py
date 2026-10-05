"""Flight Recorder: chain integrity, tamper detection, custom_id binding, blobs, races."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine, delete, update
from sqlalchemy.pool import StaticPool

from blackbox.recorder import (
    ChainBroken,
    CustomIdFormat,
    FlightRecorder,
    RecorderError,
    genesis,
    recorder_blobs,
    recorder_events,
)
from lineage.canonical import CanonicalizationError

T0 = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
FMT = CustomIdFormat(prefix="pv:", hash_hex_chars=32, max_length=127)


def new_recorder() -> FlightRecorder:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    return FlightRecorder(engine)


@pytest.fixture
def rec() -> FlightRecorder:
    return new_recorder()


def fill(rec: FlightRecorder, session: str = "s1", n: int = 4) -> list:
    return [
        rec.append(
            session, f"step.{i}", {"i": i, "note": f"event {i}"}, now=T0 + timedelta(seconds=i)
        )
        for i in range(n)
    ]


def tamper(rec: FlightRecorder, session: str, at: int, /, **values) -> None:
    with rec.engine.begin() as conn:
        conn.execute(
            update(recorder_events)
            .where(recorder_events.c.session_id == session, recorder_events.c.seq == at)
            .values(**values)
        )


# ---- chain ---------------------------------------------------------------------


def test_chain_links_from_genesis_and_verifies(rec):
    events = fill(rec)
    assert events[0].prev_hash == genesis("s1")
    for prev, cur in zip(events, events[1:], strict=False):
        assert cur.prev_hash == prev.event_hash and cur.seq == prev.seq + 1
    assert rec.verify("s1") == events[-1].event_hash == rec.head("s1").event_hash


def test_sessions_have_independent_chains(rec):
    a, b = fill(rec, "a", 2), fill(rec, "b", 2)
    assert genesis("a") != genesis("b")
    assert a[0].event_hash != b[0].event_hash
    rec.verify("a")
    rec.verify("b")


def test_rehydration_returns_exact_payloads_in_order(rec):
    fill(rec)
    assert [e.payload for e in rec.events("s1")] == [
        {"i": i, "note": f"event {i}"} for i in range(4)
    ]


def test_verify_unknown_session(rec):
    with pytest.raises(RecorderError):
        rec.verify("nope")


# ---- tampering -------------------------------------------------------------------


def test_edited_payload_is_detected(rec):
    fill(rec)
    tamper(rec, "s1", 2, payload_json=json.dumps({"i": 2, "note": "edited"}))
    with pytest.raises(ChainBroken) as e:
        rec.verify("s1")
    assert e.value.seq == 2 and "payload" in e.value.reason


def test_edited_payload_with_recomputed_payload_hash_is_detected(rec):
    from lineage.canonical import content_hash

    fill(rec)
    forged = {"i": 2, "note": "edited"}
    tamper(rec, "s1", 2, payload_json=json.dumps(forged), payload_hash=content_hash(forged))
    with pytest.raises(ChainBroken) as e:
        rec.verify("s1")
    assert e.value.seq == 2 and "event_hash" in e.value.reason


def test_rewritten_event_with_recomputed_hash_breaks_the_next_link(rec):
    """Attacker recomputes event 2's hashes consistently; event 3 no longer links to it."""
    from blackbox.recorder import compute_event_hash
    from lineage.canonical import content_hash

    events = fill(rec)
    forged = {"i": 2, "note": "edited"}
    ph = content_hash(forged)
    eh = compute_event_hash("s1", 2, "step.2", ph, events[2].recorded_at, events[2].prev_hash)
    tamper(rec, "s1", 2, payload_json=json.dumps(forged), payload_hash=ph, event_hash=eh)
    with pytest.raises(ChainBroken) as e:
        rec.verify("s1")
    assert e.value.seq == 3 and "prev_hash" in e.value.reason


def test_deleted_event_is_detected(rec):
    fill(rec)
    with rec.engine.begin() as conn:
        conn.execute(
            delete(recorder_events).where(
                recorder_events.c.session_id == "s1", recorder_events.c.seq == 1
            )
        )
    with pytest.raises(ChainBroken, match="missing event"):
        rec.verify("s1")


def test_truncated_head_changes_the_verified_hash(rec):
    """Deleting the newest event leaves a valid shorter chain, but its head no longer matches
    any custom_id bound to the removed event: resolve_custom_id catches it."""
    events = fill(rec)
    custom_id = FMT.render(events[-1].event_hash)
    with rec.engine.begin() as conn:
        conn.execute(delete(recorder_events).where(recorder_events.c.seq == 3))
    assert rec.verify("s1") == events[2].event_hash
    with pytest.raises(RecorderError, match="matches 0 events"):
        rec.resolve_custom_id("s1", custom_id, FMT)


def test_reordered_events_are_detected(rec):
    fill(rec)
    tamper(rec, "s1", 1, seq=99)
    tamper(rec, "s1", 2, seq=1)
    tamper(rec, "s1", 99, seq=2)
    with pytest.raises(ChainBroken):
        rec.verify("s1")


def test_changed_timestamp_is_detected(rec):
    events = fill(rec)
    tamper(rec, "s1", 1, recorded_at=events[1].recorded_at + timedelta(hours=1))
    with pytest.raises(ChainBroken, match="event_hash"):
        rec.verify("s1")


def test_changed_event_type_is_detected(rec):
    fill(rec)
    tamper(rec, "s1", 0, event_type="contract.passed")
    with pytest.raises(ChainBroken, match="event_hash"):
        rec.verify("s1")


@settings(max_examples=40, deadline=None)
@given(
    payloads=st.lists(
        st.dictionaries(
            st.text(max_size=5), st.integers(-1000, 1000) | st.text(max_size=8), max_size=4
        ),
        min_size=1,
        max_size=6,
    ),
    data=st.data(),
)
def test_any_single_payload_edit_is_detected(payloads, data):
    rec = new_recorder()
    for i, p in enumerate(payloads):
        rec.append("s", "e", p, now=T0 + timedelta(seconds=i))
    rec.verify("s")
    seq = data.draw(st.integers(0, len(payloads) - 1))
    edited = {**payloads[seq], "__tampered__": True}
    tamper(rec, "s", seq, payload_json=json.dumps(edited))
    with pytest.raises(ChainBroken) as e:
        rec.verify("s")
    assert e.value.seq == seq


# ---- custom_id binding -----------------------------------------------------------


def test_custom_id_binds_to_head_at_checkout_and_survives_later_events(rec):
    fill(rec, n=3)
    at_checkout = rec.head("s1")
    custom_id = FMT.render(at_checkout.event_hash)
    assert custom_id.startswith("pv:") and len(custom_id) == 3 + 32
    rec.append("s1", "paypal.order.created", {"custom_id": custom_id}, now=T0 + timedelta(1))
    assert rec.resolve_custom_id("s1", custom_id, FMT).seq == at_checkout.seq


def test_custom_id_does_not_resolve_after_tampering(rec):
    fill(rec)
    custom_id = FMT.render(rec.head("s1").event_hash)
    tamper(rec, "s1", 0, payload_json=json.dumps({"i": 0, "note": "x"}))
    with pytest.raises(ChainBroken):
        rec.resolve_custom_id("s1", custom_id, FMT)


@pytest.mark.parametrize("bad", ["xx:" + "a" * 32, "pv:" + "a" * 31, "pv:" + "G" * 32])
def test_malformed_custom_ids_rejected(rec, bad):
    fill(rec)
    with pytest.raises(RecorderError):
        rec.resolve_custom_id("s1", bad, FMT)


def test_custom_id_length_limit():
    with pytest.raises(RecorderError):
        CustomIdFormat(prefix="pv:", hash_hex_chars=64, max_length=10).render("a" * 64)


# ---- blobs, input rules, races -----------------------------------------------------


def test_blobs_are_content_addressed_and_deduplicated(rec):
    h1 = rec.put_blob(b"<html>page</html>", "text/html")
    h2 = rec.put_blob(b"<html>page</html>", "text/html")
    assert h1 == h2 and rec.get_blob(h1) == b"<html>page</html>"
    with rec.engine.connect() as conn:
        assert conn.execute(recorder_blobs.select()).all().__len__() == 1


def test_tampered_blob_is_refused(rec):
    h = rec.put_blob(b"original", "text/plain")
    with rec.engine.begin() as conn:
        conn.execute(update(recorder_blobs).values(content=b"swapped"))
    with pytest.raises(RecorderError, match="does not match"):
        rec.get_blob(h)


def test_missing_blob(rec):
    with pytest.raises(RecorderError):
        rec.get_blob("0" * 64)


def test_floats_are_refused_in_payloads(rec):
    with pytest.raises(CanonicalizationError):
        rec.append("s1", "llm.call", {"temperature": 0.7})


def test_session_and_type_required(rec):
    with pytest.raises(RecorderError):
        rec.append("", "x", {})
    with pytest.raises(RecorderError):
        rec.append("s1", "", {})


def test_concurrent_writers_never_fork_the_chain(rec):
    """A second recorder on the same database appends on top of the first's head."""
    other = FlightRecorder(rec.engine)
    for i in range(6):
        (rec if i % 2 else other).append("s1", "e", {"i": i}, now=T0 + timedelta(seconds=i))
    assert [e.payload["i"] for e in rec.events("s1")] == list(range(6))
    rec.verify("s1")


def test_lost_race_retries_on_new_head(rec, monkeypatch):
    """Simulate losing the race: the first head read is stale, so the insert collides."""
    fill(rec, n=2)
    real_head = rec.head
    stale = rec.events("s1")[0]
    calls = {"n": 0}

    def flaky_head(session_id):
        calls["n"] += 1
        return stale if calls["n"] == 1 else real_head(session_id)

    monkeypatch.setattr(rec, "head", flaky_head)
    e = rec.append("s1", "e", {"after": "race"}, now=T0 + timedelta(minutes=1))
    assert e.seq == 2 and calls["n"] == 2
    rec.verify("s1")


def test_gives_up_after_repeated_collisions(rec, monkeypatch):
    fill(rec, n=1)
    stale = rec.events("s1")[0]
    rec.max_append_attempts = 3
    monkeypatch.setattr(rec, "head", lambda _sid: None if stale.seq == 0 else stale)
    with pytest.raises(RecorderError, match="could not append"):
        rec.append("s1", "e", {})


def test_fully_relinked_history_rewrite_is_caught_by_the_paypal_anchor(rec):
    """An attacker with database access rewrites event 1 and recomputes every later hash and
    link. The chain is internally consistent again, so verify() alone passes. But the head hash
    changes, so the custom_id PayPal holds for this session no longer resolves. This is why the
    order binding exists, and why each event hash must commit to prev_hash."""
    from blackbox.recorder import compute_event_hash
    from lineage.canonical import content_hash

    events = fill(rec, n=4)
    anchor = FMT.render(events[-1].event_hash)  # what PayPal stored at checkout
    prev = events[0].event_hash
    for e in events[1:]:
        payload = {"i": e.payload["i"], "note": "rewritten"} if e.seq == 1 else e.payload
        ph = content_hash(payload)
        eh = compute_event_hash("s1", e.seq, e.event_type, ph, e.recorded_at, prev)
        tamper(
            rec,
            "s1",
            e.seq,
            payload_json=json.dumps(payload),
            payload_hash=ph,
            prev_hash=prev,
            event_hash=eh,
        )
        prev = eh
    assert rec.verify("s1") == prev != events[-1].event_hash
    with pytest.raises(RecorderError, match="matches 0 events"):
        rec.resolve_custom_id("s1", anchor, FMT)


def test_latest_events_of_a_type_across_sessions():
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from blackbox.recorder import FlightRecorder

    rec = FlightRecorder(
        create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    )
    t0 = datetime(2026, 10, 6, tzinfo=UTC)
    for i, s in enumerate(["s-a", "s-b", "s-c"]):
        rec.append(s, "paypal.order.created", {"n": i}, now=t0 + timedelta(minutes=i))
        rec.append(s, "other", {"n": i}, now=t0 + timedelta(minutes=i, seconds=1))
    got = rec.latest("paypal.order.created", limit=2)
    assert [e.session_id for e in got] == ["s-c", "s-b"]
    assert rec.latest("nothing") == []
