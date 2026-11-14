"""Agent loop tests against fake in-process systems and the in-memory store.

    .venv/bin/python -m tests.test_agent_loop      (or: pytest tests/)

Each test is one of the demo moments, checked against the fake systems' own
records (not the agent's), the same way judges would check the real thing.
"""

from __future__ import annotations

import io
import threading

from agent.clients import Systems
from agent.config import Settings, get_recall
from agent.repair import Repairer
from agent.run import Agent, Timeline
from agent.store_api import MemoryStore
from tests.fake_systems import HOSTS, FakeSystems

RID = "RC-GB-2611"


class SimulatedCrash(Exception):
    pass


def _raise_crash() -> None:
    raise SimulatedCrash()


# What a correct repair looks like for carrier v2 (docs/03-contracts.md §6).
V2_FIX = {"method": "POST", "path": "/shipments/{target_id}/return",
          "body_fields": [{"name": "recall_id", "value": "{recall_id}"},
                          {"name": "reason", "value": "{reason}"}],
          "readback_path": "/shipments/{target_id}", "readback_field": "status",
          "readback_expect": ["intercept_requested"], "explanation": "endpoint moved"}


class FakeMemory:
    """Stands in for store.memory: exact-match instead of vector search."""

    def __init__(self) -> None:
        self.fixes: dict[str, dict] = {}

    def situation_text(self, system, action, method, path, status, detail):
        return f"{system} {action}: {method} {path} returned {status} {detail}".strip()

    def find_fix(self, system, situation):
        fix = self.fixes.get(situation)
        return {"fix": fix, "score": 0.93} if fix else None

    def save_fix(self, system, action, situation, fix, source_recall):
        is_new = situation not in self.fixes
        self.fixes.setdefault(situation, fix)
        return is_new


class FakeLLM:
    def __init__(self, answer: dict) -> None:
        self.answer, self.calls = answer, 0

    def __call__(self, spec, result, api_spec):
        self.calls += 1
        assert result.status == 404 and "shipment_ref" in str(api_spec)
        return dict(self.answer)


def make_agent(fake: FakeSystems, store: MemoryStore, memory=None, llm=None, **settings) -> Agent:
    systems = Systems(HOSTS, transport=fake.transport)
    return Agent(
        recall=get_recall(RID),
        store=store,
        systems=systems,
        settings=Settings(urls=HOSTS, **settings),
        log=Timeline(io.StringIO()),
        crash=_raise_crash,
        repairer=Repairer(systems, memory, llm or FakeLLM(V2_FIX)),
        pause_poll_s=0.01,
    )


def test_happy_path() -> None:
    fake, store = FakeSystems(), MemoryStore()
    report = make_agent(fake, store).run()

    assert report["status"] == "done", report
    assert report["units_held"] == {"verified": 20, "total": 20}
    assert report["orders_cancelled"] == {"verified": 10, "total": 10, "escalated": 0}
    assert report["shipments_intercepted"] == {"verified": 8, "total": 8}
    assert report["customers_notified"] == {"verified": 22, "total": 22}
    assert report["denied_targets"] == 1  # decoy UNIT-0099 from lot GB-2612
    assert fake.units["UNIT-0099"]["status"] == "available"
    assert report["duplicate_notifications"] == 0
    steps = store.steps_in_status(RID, "verified")
    assert [s["seq"] for s in steps] == list(range(1, 61))
    assert all(s["proof"]["source"] == "read_back" for s in steps)
    assert store.control[RID]["status"] == "done"
    # labels waiting for pickup are not intercepted (their orders are cancelled instead)
    assert all(fake.shipments[f"TRK-{7000 + i}"]["status"] == "label_created" for i in range(1, 11))
    assert report["scope_checks"][0]["ok"] and report["needs_attention"] == []
    scope = next(a["detail"]["scope"] for a in store.audit if "scope" in (a.get("detail") or {}))
    decisions = {(o["status"], o["decision"]) for o in scope["orders"]}
    assert decisions == {("open", "cancel"), ("shipped", "intercept_shipment"),
                         ("delivered", "notify_only")}
    assert len(scope["customers"]) == 22


def test_order_shipped_before_cancel_is_intercepted_instead() -> None:
    fake, store = FakeSystems(), MemoryStore()
    fake.ship_on_first_cancel = True  # the truck collects ORD-1010 while Tracer is cancelling
    report = make_agent(fake, store).run()

    assert report["status"] == "done", report
    assert report["escalations"] == 1
    assert report["orders_cancelled"] == {"verified": 9, "total": 10, "escalated": 1}
    assert report["shipments_intercepted"] == {"verified": 9, "total": 9}
    assert fake.shipments["TRK-7010"]["status"] == "intercept_requested"
    assert fake.notices_per_customer(RID)["CUST-110"] == 1
    assert report["needs_attention"] == []


def test_shipped_order_unknown_to_carrier_is_flagged_for_a_human() -> None:
    fake, store = FakeSystems(), MemoryStore()
    del fake.shipments["TRK-5003"]  # order ORD-1013 says shipped; the carrier lost it
    report = make_agent(fake, store).run()

    assert not report["scope_checks"][0]["ok"]
    assert any("ORD-1013" in (i["last_error"] or "") for i in report["needs_attention"])
    assert fake.notices_per_customer(RID)["CUST-113"] == 1  # the customer is still warned


def test_silent_failure_is_caught_and_retried() -> None:
    fake, store = FakeSystems(), MemoryStore()
    fake.drop_next_writes["warehouse"] = 2  # 200 OK, but nothing changes
    report = make_agent(fake, store).run()

    assert report["status"] == "done", report
    first = store.steps["RC-GB-2611:hold:UNIT-0001"]
    assert first["attempts"] == 3 and first["status"] == "verified"
    assert report["failed_attempts"] == 2
    assert all(u["status"] == "hold" for k, u in fake.units.items() if k != "UNIT-0099")


def test_crash_after_call_reconciles_without_resending() -> None:
    fake, store = FakeSystems(), MemoryStore()
    try:
        make_agent(fake, store, crash_at_seq=45).run()
        raise AssertionError("expected the agent to crash at seq 45")
    except SimulatedCrash:
        pass
    in_doubt = store.steps_in_status(RID, "sent")
    assert [s["seq"] for s in in_doubt] == [45]

    report = make_agent(fake, store).run()  # restart, same worker id
    assert report["status"] == "done", report
    step45 = next(s for s in store.steps.values() if s["seq"] == 45)
    assert step45["proof"]["source"] == "reconcile" and step45["attempts"] == 1
    assert report["reconciled_after_crash"] == 1
    per_customer = fake.notices_per_customer(RID)
    assert len(per_customer) == 22 and set(per_customer.values()) == {1}


def test_crash_on_demand_lands_mid_step_once() -> None:
    fake, store = FakeSystems(), MemoryStore()
    store.ensure_control(RID, "GB-2611")
    store.control[RID]["crash"] = True  # the live log's "Crash the agent" button
    try:
        make_agent(fake, store).run()
        raise AssertionError("expected a crash")
    except SimulatedCrash:
        pass
    assert [s["seq"] for s in store.steps_in_status(RID, "sent")] == [1]  # half-done
    report = make_agent(fake, store).run()  # flag was consumed: no second crash
    assert report["status"] == "done" and report["reconciled_after_crash"] == 1


def test_crash_before_call_lands_is_resent_once() -> None:
    fake, store = FakeSystems(), MemoryStore()
    agent = make_agent(fake, store)
    agent.run()
    # Simulate "marked sent, then died before the POST": a sent step with no effect.
    target = store.steps["RC-GB-2611:notify:CUST-105"]
    fake.notifications = [n for n in fake.notifications if n["customer_id"] != "CUST-105"]
    target.update(status="sent")

    make_agent(fake, store).run()
    assert fake.notices_per_customer(RID)["CUST-105"] == 1
    assert store.steps["RC-GB-2611:notify:CUST-105"]["status"] == "verified"


def test_second_worker_cannot_take_a_live_lease() -> None:
    fake, store = FakeSystems(), MemoryStore()
    store.ensure_control(RID, "GB-2611")
    assert store.claim_lease(RID, "worker-1")
    report = make_agent(fake, store, worker_id="worker-2").run()
    assert report == {"status": "lease_busy"}
    assert fake.notifications == []


def test_pause_waits_then_resumes_from_same_step() -> None:
    fake, store = FakeSystems(), MemoryStore()
    store.ensure_control(RID, "GB-2611")
    store.control[RID]["paused"] = True
    held_while_paused: list[int] = []

    def human_unpauses() -> None:
        import time
        time.sleep(0.2)
        held_while_paused.append(sum(u["status"] == "hold" for u in fake.units.values()))
        store.control[RID]["paused"] = False

    threading.Thread(target=human_unpauses).start()
    report = make_agent(fake, store).run()
    assert held_while_paused == [0]  # nothing happened while paused
    assert report["status"] == "done"
    assert store.count_events(RID, "paused") == 1


def test_api_change_repaired_by_llm_and_saved_to_memory() -> None:
    fake, store, memory, llm = FakeSystems(), MemoryStore(), FakeMemory(), FakeLLM(V2_FIX)
    fake.carrier_version = "v2"
    report = make_agent(fake, store, memory, llm).run()

    assert report["status"] == "done", report
    assert report["shipments_intercepted"] == {"verified": 8, "total": 8}
    assert llm.calls == 1 and report["llm_repairs"] == 1 and report["memory_hits"] == 0
    assert len(memory.fixes) == 1  # saved only after the read-back passed
    first = store.steps["RC-GB-2611:intercept:TRK-5001"]
    assert first["attempts"] == 2  # 404, then the fix tested on this one shipment
    later = store.steps["RC-GB-2611:intercept:TRK-5008"]
    assert later["attempts"] == 1  # every later truck uses the fix straight away
    assert all(fake.shipments[f"TRK-{5000 + i}"]["status"] == "intercept_requested"
               for i in range(1, 9))


def test_second_recall_uses_memory_not_llm() -> None:
    memory = FakeMemory()
    first = FakeSystems()
    first.carrier_version = "v2"
    make_agent(first, MemoryStore(), memory, FakeLLM(V2_FIX)).run()

    again, llm = FakeSystems(), FakeLLM(V2_FIX)
    again.carrier_version = "v2"
    report = make_agent(again, MemoryStore(), memory, llm).run()
    assert report["status"] == "done", report
    assert llm.calls == 0 and report["memory_hits"] == 1 and report["llm_repairs"] == 0


def test_unsafe_llm_fix_is_rejected_and_left_for_a_human() -> None:
    fake, store, memory = FakeSystems(), MemoryStore(), FakeMemory()
    fake.carrier_version = "v2"
    # A "fix" that would count any status as proof: the guard must refuse it.
    sloppy = {**V2_FIX, "readback_expect": ["in_transit", "intercept_requested"]}
    report = make_agent(fake, store, memory, FakeLLM(sloppy)).run()

    assert report["status"] == "incomplete"
    assert report["shipments_intercepted"]["verified"] == 0
    assert report["customers_notified"] == {"verified": 22, "total": 22}  # the rest still ran
    assert memory.fixes == {}  # nothing unproven is remembered
    assert {i["target_id"] for i in report["needs_attention"]} == {
        f"TRK-{5000 + i}" for i in range(1, 9)}
    assert all(fake.shipments[f"TRK-{5000 + i}"]["status"] == "in_transit" for i in range(1, 9))


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"PASS  {name}")
    print(f"{len(tests)} passed")
