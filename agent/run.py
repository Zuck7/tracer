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
from agent.plan import build_plan, cross_check, new_step
from agent.reconcile import proof_from, reconcile, step_values
from agent.repair import REPAIRABLE_STATUS, Proposal, Repairer, guard
from agent.store_api import MEMORY_STORE_PATH, MemoryStore, Step, StoreAPI, load_store, now

_COLORS = {"verified": "32", "failed": "31", "sent": "33", "DENIED": "35", "DUPLICATE": "41",
           "repair": "36", "skipped": "35"}
# last_error prefixes of steps that were handled another way (not a failure to chase)
RESOLVED_ELSEWHERE = ("already shipped: escalated", "already delivered:")
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
    repairer: Repairer | None = None
    pause_poll_s: float = 0.5
    specs: dict[str, dict[str, Any]] = field(default_factory=default_specs)
    # A repaired CallSpec under test, per action. Adopted only after a read-back passes.
    trials: dict[str, Proposal] = field(default_factory=dict)
    # Actions we stopped trying (a repair failed). Their steps stay planned for a human.
    blocked: dict[str, str] = field(default_factory=dict)

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
            if self.store.is_paused(self.rid) and not self.wait_while_paused(step):
                return {"status": "lease_lost"}
            if not self.store.renew_lease(self.rid, self.settings.worker_id):
                self.log.info("lost the lease: another worker took over, stopping", "failed")
                return {"status": "lease_lost"}
            self.process_step(step)
            if self.settings.step_delay_ms:
                time.sleep(self.settings.step_delay_ms / 1000)

        return self.report(time.monotonic() - started)

    def wait_while_paused(self, step: Step) -> bool:
        """A human paused the recall: stop before the next step, hold the lease, and
        carry on from the same step when they unpause. False if the lease was lost."""
        self.store.log_event(self.rid, "paused", self.actor, step)
        self.log.info(f"paused by a human before step #{step['seq']}: waiting", "failed")
        while self.store.is_paused(self.rid):
            if not self.store.renew_lease(self.rid, self.settings.worker_id):
                self.log.info("lost the lease while paused, stopping", "failed")
                return False
            time.sleep(self.pause_poll_s)
        self.log.info(f"resumed at step #{step['seq']}")
        return True

    def plan(self) -> None:
        plan = build_plan(self.recall, self.systems)
        # What was found where, and what was decided for each item, with reasons.
        self.store.log_event(self.rid, "planned", self.actor, detail={"scope": plan.scope})
        for system in ("warehouse", "orders", "carrier", "customers"):
            found: dict[str, int] = {}
            for item in plan.scope[system]:
                label = f"{item.get('status') or item.get('order_status')} → {item['decision']}"
                found[label] = found.get(label, 0) + 1
            self.log.info(f"found in {system}: " + ", ".join(f"{n} {k}" for k, n in found.items()))
        for check in plan.scope["checks"]:
            self.log.info(f"check: {check['summary']}", "verified" if check["ok"] else "failed")
            for problem in check["problems"]:
                self.log.info(f"  needs a human: {problem}", "failed")
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

        action = step["action"]
        if (step.get("last_error") or "").startswith(RESOLVED_ELSEWHERE):
            return step  # e.g. a cancel that became an intercept: already handled
        if action in self.blocked:
            self.log.step(step, "skipped", f"left for a human: {self.blocked[action]}")
            return step

        values = step_values(self.recall, step)
        while step["attempts"] < self.settings.max_attempts:
            retrying = step["attempts"] > 0
            trial = self.trials.get(action)
            spec = trial.spec if trial else self.specs[action]

            # Write-ahead: the step is `sent` in the database BEFORE the call.
            step = self.store.mark_sent(step, self.actor, decision.reason)
            note = f" (trying the {trial.source} fix)" if trial else ""
            self.log.step(step, "sent", f"attempt {step['attempts']}{note}")

            # On a retry, check first: the last attempt may have landed after all.
            if retrying:
                read_back = self.systems.read_back(step["system"], spec, values)
                if self._settle(step, read_back, source="reconcile"):
                    if trial:
                        self._adopt(step, trial)
                    return step

            result = self.systems.send(step["system"], spec, values, step["idempotency_key"])

            planned_crash = self.settings.crash_at_seq == step["seq"] and step["attempts"] == 1
            if planned_crash or self._crash_requested():
                self.log.info(f"crash at step #{step['seq']}: agent killed after the call, "
                              "before the read-back", "failed")
                self.crash()

            if not result.ok:
                if self._escalate_shipped_order(step, result):
                    return step
                step = self.store.mark_failed(step, result.describe(), self.actor)
                self.log.step(step, "failed", result.describe())
                if trial:
                    self._reject(step, trial, f"the fix got {result.describe()}")
                    return step
                if self.repairer and result.status in REPAIRABLE_STATUS:
                    if self._propose_repair(step, spec, result):
                        continue  # try the proposed fix on this same step
                    return step
                if not result.retryable:
                    return step  # e.g. 409 "already shipped": resending won't change that
                self._backoff(step)
                continue

            read_back = self.systems.read_back(step["system"], spec, values)
            if self._settle(step, read_back, source="read_back"):
                if trial:
                    self._adopt(step, trial)
                return step
            error = f"{result.status} but read-back shows {read_back.observed}"
            step = self.store.mark_failed(step, f"read-back mismatch: {error}", self.actor)
            self.log.step(step, "failed", error)
            if trial:
                self._reject(step, trial, f"read-back after the fix shows {read_back.observed}")
                return step
            self._backoff(step)

        self.log.step(step, "failed", f"gave up after {step['attempts']} attempts")
        return step

    def _escalate_shipped_order(self, step: Step, result) -> bool:
        """A truck collected the order before we could cancel it (409 "already shipped").
        Cancelling is impossible now, so do what a recall coordinator would: stop the
        shipment instead. If it was already delivered, the customer notice covers it."""
        if step["action"] != "cancel_order" or result.status != 409:
            return False
        try:
            order = self.systems.get_json("orders", f"/orders/{step['target_id']}")
        except Exception:  # noqa: BLE001 - can't see the order: leave it failed for a human
            return False
        status, tracking = order.get("status"), order.get("tracking_no")
        if status == "delivered":
            self.store.mark_failed(step, "already delivered: customer is on the notice list",
                                   self.actor)
            self.log.step(step, "repair", "already delivered: the customer notice covers it")
            return True
        if status != "shipped" or not tracking:
            return False
        self.store.mark_failed(step, f"already shipped: escalated to intercept {tracking}",
                               self.actor)
        self.store.log_event(self.rid, "planned", self.actor, step, detail={"escalation": {
            "order_id": step["target_id"], "tracking_no": tracking, "order_status": status,
            "reason": f"{step['target_id']} left on truck {tracking} before it could be "
                      "cancelled, so Tracer is stopping the shipment instead."}})
        intercept = new_step(self.recall, self.store.count_steps(self.rid) + 1,
                             "intercept_shipment", tracking, order.get("lot"))
        self.store.insert_plan([intercept])
        self.log.step(step, "repair", f"too late to cancel: on truck {tracking}, "
                      f"intercepting it instead (new step #{intercept['seq']})")
        created = next((s for s in self.store.steps_in_status(self.rid, "planned")
                        if s["idempotency_key"] == intercept["idempotency_key"]), None)
        if created:
            self.process_step(created)  # the truck is moving: don't wait for the queue
        return True

    def _crash_requested(self) -> bool:
        """Demo crash on demand (PROJECT.md §9): someone set control.crash in Atlas.
        Consumed atomically, so the restarted agent doesn't crash again."""
        take = getattr(self.store, "take_crash_request", None)
        return bool(take and take(self.rid))

    # -- repair ----------------------------------------------------------------
    def _propose_repair(self, step: Step, spec: dict[str, Any], result) -> bool:
        """Memory first, then Claude. True if there's a guarded fix to try on this step."""
        assert self.repairer is not None
        system, action = step["system"], step["action"]
        situation = self.repairer.situation(system, action, spec, result)
        proposal = self.repairer.from_memory(system, situation)
        if proposal:
            self.store.log_event(self.rid, "memory_hit", self.actor, step,
                                 detail={"score": proposal.score, "situation": situation})
            self.log.step(step, "repair",
                          f"fix found in memory (score {proposal.score:.2f}), no LLM call")
        else:
            self.log.step(step, "repair", "API changed: asking Claude for a fix")
            try:
                proposal = self.repairer.from_llm(system, spec, result, situation)
            except Exception as exc:  # noqa: BLE001 - any failure here means "no fix"
                return self._give_up_repair(step, "llm", f"no fix: {type(exc).__name__}: {exc}")
            why = f": {proposal.explanation}" if proposal.explanation else ""
            self.log.step(step, "repair", f"Claude proposes {proposal.spec['method']} "
                          f"{proposal.spec['path']}{why}")
        self.store.log_event(self.rid, "repair_proposed", self.actor, step, detail={
            "source": proposal.source, "situation": situation, "spec": proposal.spec,
            "explanation": proposal.explanation, "score": proposal.score})

        errors = guard(proposal.spec, self.specs[action])
        if errors:
            return self._give_up_repair(step, proposal.source, "; ".join(errors))
        self.trials[action] = proposal
        return True

    def _adopt(self, step: Step, trial: Proposal) -> None:
        """The fix passed a read-back on a real record: use it for every later step."""
        action = step["action"]
        self.specs[action] = trial.spec
        del self.trials[action]
        saved = self.repairer.remember(trial, step["system"], action, self.rid) \
            if self.repairer else False
        self.store.log_event(self.rid, "repair_verified", self.actor, step, detail={
            "source": trial.source, "target_id": step["target_id"], "saved_to_memory": saved})
        where = "saved to memory" if saved else f"from {trial.source}"
        self.log.step(step, "repair", f"fix verified on {step['target_id']}, {where}; "
                      f"using it for the rest of {action}")

    def _reject(self, step: Step, trial: Proposal, reason: str) -> None:
        del self.trials[step["action"]]
        self._give_up_repair(step, trial.source, reason)

    def _give_up_repair(self, step: Step, source: str, reason: str) -> bool:
        """Never keep guessing against a live system: stop this action, leave it to a human."""
        self.store.log_event(self.rid, "repair_rejected", self.actor, step,
                             detail={"source": source, "reason": reason})
        self.blocked[step["action"]] = f"repair rejected ({reason})"
        self.log.step(step, "failed", f"repair rejected: {reason}")
        return False

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
                           ("reconciled_after_crash", "reconciled"), ("memory_hits", "memory_hit"),
                           ("repairs_verified", "repair_verified")):
            report[key] = self.store.count_events(self.rid, event)
        # Every proposal comes from memory or from the LLM.
        report["llm_repairs"] = (self.store.count_events(self.rid, "repair_proposed")
                                 - report["memory_hits"])
        failed = self.store.steps_in_status(self.rid, "failed")
        escalated = [s for s in failed if (s.get("last_error") or "").startswith(RESOLVED_ELSEWHERE)]
        report["orders_cancelled"]["escalated"] = len(escalated)
        report["escalations"] = sum(1 for s in escalated
                                    if s["last_error"].startswith("already shipped"))
        report["needs_attention"] = [
            {k: s.get(k) for k in ("seq", "action", "target_id", "status", "last_error")}
            for status in ("failed", "sent", "planned")
            for s in self.store.steps_in_status(self.rid, status) if s not in escalated
        ]
        try:  # re-run the cross-check against the systems as they are now
            report["scope_checks"] = cross_check(self.recall, self.systems)
        except Exception:  # noqa: BLE001 - a report must not crash the run
            report["scope_checks"] = []
        for check in report["scope_checks"]:
            report["needs_attention"] += [{"seq": None, "action": "cross_check", "target_id": None,
                                           "status": "unmatched", "last_error": p}
                                          for p in check["problems"]]
        intercepts = [s for s in self.store.steps_in_status(self.rid, "verified")
                      if s["action"] == "intercept_shipment"]
        last = max((s["proof"]["at"] for s in intercepts), default=None)
        report["last_intercept_at"] = last.isoformat() if hasattr(last, "isoformat") else last
        report["run_seconds"] = round(run_seconds, 1)
        report["generated_at"] = now()

        done = all(v["verified"] + v.get("escalated", 0) == v["total"]
                   for k, v in report.items() if k in _ACTION_TOTALS.values())
        report["status"] = "done" if done else "incomplete"
        if done:
            self.store.finish(self.rid, report)
        else:
            # Keep the report for the people who must finish the job by hand.
            save = getattr(self.store, "save_report", None)
            if save:
                save(self.rid, report)
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
            extra = report[key].get("escalated")
            note = f"  (+{extra} shipped first: intercepted instead)" if extra else ""
            self.log.info(f"  {key:<24} {report[key]['verified']}/{report[key]['total']}{note}")
        for key in ("escalations", "duplicate_notifications", "denied_targets", "failed_attempts",
                    "reconciled_after_crash", "llm_repairs", "memory_hits", "run_seconds"):
            self.log.info(f"  {key:<24} {report[key]}")
        for item in report["needs_attention"]:
            self.log.info(f"  needs a human: #{item['seq']} {item['action']} {item['target_id']} "
                          f"({item['status']}: {item['last_error'] or 'not attempted'})", "failed")


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
        store = load_store(args.store)
        memory = None
        if not isinstance(store, MemoryStore):  # vector memory lives in Atlas
            from store import memory  # type: ignore[no-redef]
        agent = Agent(get_recall(args.recall), store, systems, settings,
                      repairer=Repairer(systems, memory))
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
