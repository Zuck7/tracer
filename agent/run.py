"""The recall agent's main loop.

    python -m agent.run --recall RC-GB-2611 [--store auto|atlas|memory]

One run: claim the lease → plan (first run only) → reconcile leftovers from a
crash → work through every planned/failed step in seq order → report.

Per step (docs/02-architecture.md, Flow 1):
    policy check → mark_sent (saved BEFORE the call) → call with the step's
    idempotency key → read the record back → verified (with proof) or failed.
A failed step is retried with the same key, up to MAX_ATTEMPTS. Before a
retry we read back first, in case the previous attempt actually landed.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import httpx

from agent import policy
from agent.callspecs import default_specs
from agent.clients import ReadBack, Systems
from agent.config import Recall, Settings, get_recall
from agent.plan import build_plan
from agent.reconcile import proof_from, reconcile, step_values
from agent.store_api import MEMORY_STORE_PATH, Step, StoreAPI, load_store, now

_COLORS = {"verified": "32", "failed": "31", "sent": "33", "DENIED": "35", "DUPLICATE": "41"}
_ACTION_TOTALS = {
    "hold_inventory": "units_held",
    "cancel_order": "orders_cancelled",
    "intercept_shipment": "shipments_intercepted",
    "notify_customer": "customers_notified",
}


class Timeline:
    """Live log in the terminal, one line per step event."""

    def __init__(self, stream=sys.stdout) -> None:
        self.stream = stream
        self.color = stream.isatty()

    def _paint(self, text: str, status: str) -> str:
        code = _COLORS.get(status)
        return f"\033[{code}m{text}\033[0m" if code and self.color else text

    def step(self, step: Step, status: str, detail: str = "") -> None:
        clock = datetime.now().strftime("%H:%M:%S")
        line = (f"{clock}  #{step['seq']:<3} {step['system']:<9} {step['target_id']:<10} "
                f"{self._paint(f'{status:<9}', status)} {detail}")
        print(line.rstrip(), file=self.stream, flush=True)

    def info(self, message: str, status: str = "") -> None:
        clock = datetime.now().strftime("%H:%M:%S")
        print(f"{clock}  {self._paint(message, status)}", file=self.stream, flush=True)


def hard_crash() -> None:
    """Simulated crash for the demo: die instantly, no cleanup, no finally blocks."""
    os._exit(137)


@dataclass
class Agent:
    recall: Recall
    store: StoreAPI
    systems: Systems
    settings: Settings
    log: Timeline = field(default_factory=Timeline)
    crash: Callable[[], None] = hard_crash
    specs: dict[str, dict[str, Any]] = field(default_factory=default_specs)

    @property
    def rid(self) -> str:
        return self.recall.recall_id

    @property
    def actor(self) -> str:
        return self.settings.actor

    # -- the run -------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        self.store.ensure_control(self.rid, self.recall.lot)
        if not self.store.claim_lease(self.rid, self.settings.worker_id):
            self.log.info(f"{self.rid}: another worker holds the lease, not starting", "failed")
            return {"status": "lease_busy"}
        self.store.log_event(self.rid, "lease_claimed", self.actor)
        self.log.info(f"{self.rid} ({self.recall.product}, lot {self.recall.lot}): "
                      f"lease claimed by {self.settings.worker_id}")

        existing = self.store.count_steps(self.rid)
        if existing == 0:
            self.plan()
        else:
            self.log.info(f"resuming: {existing} steps already planned")

        found = reconcile(self.recall, self.store, self.systems, self.specs, self.actor,
                          self.log.step)
        if found:
            self.log.info(f"reconciled {found} half-done step(s): already done, not resent")

        for step in self.store.next_steps(self.rid, self.settings.max_attempts):
            if self.store.is_paused(self.rid):
                self.store.log_event(self.rid, "paused", self.actor, step)
                self.log.info(f"paused before step #{step['seq']}: stopping", "failed")
                return {"status": "paused"}
            if not self.store.renew_lease(self.rid, self.settings.worker_id):
                self.log.info("lost the lease: another worker took over, stopping", "failed")
                return {"status": "lease_lost"}
            self.process_step(step)
            if self.settings.step_delay_ms:
                time.sleep(self.settings.step_delay_ms / 1000)

        return self.report(time.monotonic() - started)

    def plan(self) -> None:
        plan = build_plan(self.recall, self.systems)
        for denied in plan.denied:
            self.store.log_event(self.rid, "denied", self.actor, policy_decision="deny",
                                 policy_reason=denied["reason"], detail=denied)
            self.log.info(f"DENIED {denied['system']} {denied['target_id']}: {denied['reason']}",
                          "DENIED")
        inserted = self.store.insert_plan(plan.steps)
        self.log.info(f"planned {inserted} steps, denied {len(plan.denied)} target(s)")

    # -- one step ------------------------------------------------------------
    def process_step(self, step: Step) -> Step:
        decision = policy.check(self.recall, step["system"], step["action"], step.get("lot"))
        if not decision.allow:
            self.store.log_event(self.rid, "denied", self.actor, step, policy_decision="deny",
                                 policy_reason=decision.reason)
            self.log.step(step, "DENIED", decision.reason)
            return step

        values = step_values(self.recall, step)
        while step["attempts"] < self.settings.max_attempts:
            retrying = step["attempts"] > 0
            spec = self.specs[step["action"]]

            # Write-ahead: the step is `sent` in the database BEFORE the call.
            step = self.store.mark_sent(step, self.actor, decision.reason)
            self.log.step(step, "sent", f"attempt {step['attempts']}")

            # On a retry, check first: the last attempt may have landed after all.
            if retrying:
                read_back = self.systems.read_back(step["system"], spec, values)
                if self._settle(step, read_back, source="reconcile"):
                    return step

            result = self.systems.send(step["system"], spec, values, step["idempotency_key"])

            if self.settings.crash_at_seq == step["seq"] and step["attempts"] == 1:
                self.log.info(f"CRASH_AT_SEQ={step['seq']}: agent killed after the call, "
                              "before the read-back", "failed")
                self.crash()

            if not result.ok:
                step = self.store.mark_failed(step, result.describe(), self.actor)
                self.log.step(step, "failed", result.describe())
                if not result.retryable:
                    return step  # a 4xx won't fix itself by resending (repair hooks in here)
                self._backoff(step)
                continue

            read_back = self.systems.read_back(step["system"], spec, values)
            if self._settle(step, read_back, source="read_back"):
                return step
            error = f"{result.status} but read-back shows {read_back.observed}"
            step = self.store.mark_failed(step, f"read-back mismatch: {error}", self.actor)
            self.log.step(step, "failed", error)
            self._backoff(step)

        self.log.step(step, "failed", f"gave up after {step['attempts']} attempts")
        return step

    def _settle(self, step: Step, read_back: ReadBack, source: str) -> bool:
        """Close the step if the read-back proves it done (or proves a duplicate)."""
        if read_back.duplicate:
            self.store.mark_failed(step, f"duplicate: {read_back.observed}", self.actor,
                                   event="duplicate_detected")
            self.log.step(step, "DUPLICATE", read_back.observed)
            return True
        if not read_back.verified:
            return False
        event = "reconciled" if source == "reconcile" else "verified"
        self.store.mark_verified(step, proof_from(read_back, step, source), self.actor, event)
        note = " (already done, not resent)" if source == "reconcile" else ""
        self.log.step(step, "verified", f"proof: {read_back.observed}{note}")
        return True

    @staticmethod
    def _backoff(step: Step) -> None:
        time.sleep(min(0.2 * step["attempts"], 1.0))

    # -- report --------------------------------------------------------------
    def report(self, run_seconds: float) -> dict[str, Any]:
        counts = self.store.status_counts(self.rid)
        report: dict[str, Any] = {"recall_id": self.rid, "lot": self.recall.lot}
        for action, key in _ACTION_TOTALS.items():
            per_status = counts.get(action, {})
            report[key] = {"verified": per_status.get("verified", 0),
                           "total": sum(per_status.values())}
        report["duplicate_notifications"] = self._duplicate_notifications()
        for key, event in (("denied_targets", "denied"), ("failed_attempts", "failed"),
                           ("reconciled_after_crash", "reconciled")):
            report[key] = self.store.count_events(self.rid, event)
        report["run_seconds"] = round(run_seconds, 1)
        report["generated_at"] = now()

        done = all(v["verified"] == v["total"] for k, v in report.items()
                   if k in _ACTION_TOTALS.values())
        report["status"] = "done" if done else "incomplete"
        if done:
            self.store.finish(self.rid, report)
        self._print_report(report)
        return report

    def _duplicate_notifications(self) -> int | None:
        """Counted from the notify system itself, not our own records."""
        try:
            sent = self.systems.get_json("notify", "/notifications", {"recall_id": self.rid})
        except Exception:  # noqa: BLE001 - a report must not crash the run
            return None
        per_customer: dict[str, int] = {}
        for notice in sent:
            per_customer[notice["customer_id"]] = per_customer.get(notice["customer_id"], 0) + 1
        return sum(n - 1 for n in per_customer.values() if n > 1)

    def _print_report(self, report: dict[str, Any]) -> None:
        status = "verified" if report["status"] == "done" else "failed"
        self.log.info(f"{self.rid} {report['status'].upper()}", status)
        for key in _ACTION_TOTALS.values():
            self.log.info(f"  {key:<24} {report[key]['verified']}/{report[key]['total']}")
        for key in ("duplicate_notifications", "denied_targets", "failed_attempts",
                    "reconciled_after_crash", "run_seconds"):
            self.log.info(f"  {key:<24} {report[key]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a Tracer recall.")
    parser.add_argument("--recall", default="RC-GB-2611")
    parser.add_argument("--store", choices=["auto", "atlas", "memory"], default="auto",
                        help="auto = Atlas if store/ exists, else a local JSON stand-in")
    parser.add_argument("--reset-memory-store", action="store_true",
                        help="delete the local JSON stand-in store before running")
    args = parser.parse_args(argv)

    if args.reset_memory_store and MEMORY_STORE_PATH.exists():
        MEMORY_STORE_PATH.unlink()
    settings = Settings.from_env()
    systems = Systems(settings.urls)
    try:
        agent = Agent(get_recall(args.recall), load_store(args.store), systems, settings)
        report = agent.run()
    except httpx.ConnectError as exc:
        print(f"Can't reach {exc.request.url.scheme}://{exc.request.url.netloc.decode()}. "
              "Are the mocks running? (uvicorn mocks.warehouse:app --port 8001, ...)",
              file=sys.stderr)
        return 2
    finally:
        systems.close()
    return 0 if report.get("status") == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
