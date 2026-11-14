"""Agent loop tests against fake in-process systems and the in-memory store.

    .venv/bin/python -m tests.test_agent_loop      (or: pytest tests/)

Each test is one of the demo moments, checked against the fake systems' own
records (not the agent's), the same way judges would check the real thing.
"""

from __future__ import annotations

import io

from agent.clients import Systems
from agent.config import Settings, get_recall
from agent.run import Agent, Timeline
from agent.store_api import MemoryStore
from tests.fake_systems import HOSTS, FakeSystems

RID = "RC-GB-2611"


class SimulatedCrash(Exception):
    pass


def _raise_crash() -> None:
    raise SimulatedCrash()


def make_agent(fake: FakeSystems, store: MemoryStore, **settings) -> Agent:
    return Agent(
        recall=get_recall(RID),
        store=store,
        systems=Systems(HOSTS, transport=fake.transport),
        settings=Settings(urls=HOSTS, **settings),
        log=Timeline(io.StringIO()),
        crash=_raise_crash,
    )


def test_happy_path() -> None:
    fake, store = FakeSystems(), MemoryStore()
    report = make_agent(fake, store).run()

    assert report["status"] == "done", report
    assert report["units_held"] == {"verified": 20, "total": 20}
    assert report["orders_cancelled"] == {"verified": 10, "total": 10}
    assert report["shipments_intercepted"] == {"verified": 8, "total": 8}
    assert report["customers_notified"] == {"verified": 22, "total": 22}
    assert report["denied_targets"] == 1  # decoy UNIT-0099 from lot GB-2612
    assert fake.units["UNIT-0099"]["status"] == "available"
    assert report["duplicate_notifications"] == 0
    steps = store.steps_in_status(RID, "verified")
    assert [s["seq"] for s in steps] == list(range(1, 61))
    assert all(s["proof"]["source"] == "read_back" for s in steps)
    assert store.control[RID]["status"] == "done"


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


def test_pause_stops_before_next_step() -> None:
    fake, store = FakeSystems(), MemoryStore()
    store.ensure_control(RID, "GB-2611")
    store.control[RID]["paused"] = True
    report = make_agent(fake, store).run()
    assert report == {"status": "paused"}
    assert store.steps_in_status(RID, "verified") == []


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"PASS  {name}")
    print(f"{len(tests)} passed")
