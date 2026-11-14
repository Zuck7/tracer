"""Policy gate: checked before a target becomes a step, and again before every write.

Why a gate: the warehouse lot search is fuzzy and also returns a unit from lot
GB-2612. Holding it would freeze good stock. The gate is plain code, not the
LLM, so its decisions are predictable and every one lands in the audit log.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.callspecs import ACTIONS
from agent.config import Recall


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str

    @property
    def label(self) -> str:
        return "allow" if self.allow else "deny"


def check(recall: Recall, system: str, action: str, target_lot: str | None) -> Decision:
    if action not in ACTIONS:
        return Decision(False, f"action {action!r} is not a recall action")
    expected_system = ACTIONS[action][0]
    if system != expected_system:
        return Decision(False, f"{action} must go to {expected_system}, not {system!r}")
    if target_lot != recall.lot:
        return Decision(False, f"lot {target_lot} is outside recall scope ({recall.lot})")
    return Decision(True, f"lot {recall.lot} in recall scope")
