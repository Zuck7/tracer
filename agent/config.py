"""Agent settings and the recall catalogue.

Settings come from environment variables (docs/contracts.md §9), so the demo
can slow the loop down (STEP_DELAY_MS) or force a crash (CRASH_AT_SEQ) without
touching code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


@dataclass(frozen=True)
class Recall:
    recall_id: str
    lot: str
    product: str
    hazard: str

    @property
    def reason(self) -> str:
        """Sent with every hold / cancel / intercept, so the other system knows why."""
        return f"{self.hazard}, lot {self.lot}"

    @property
    def message(self) -> str:
        """The customer notice."""
        return (
            f"Recall notice: {self.product} from lot {self.lot} "
            f"({self.hazard.lower()}). Do not eat them. Return them for a full refund."
        )


# Synthetic recalls, matching the seed data in docs/contracts.md §7.
RECALLS: dict[str, Recall] = {
    "RC-GB-2611": Recall("RC-GB-2611", "GB-2611", "granola bars", "Undeclared peanuts"),
    "RC-OB-3107": Recall("RC-OB-3107", "OB-3107", "oat bars", "Undeclared tree nuts"),
}


def get_recall(recall_id: str) -> Recall:
    try:
        return RECALLS[recall_id]
    except KeyError:
        known = ", ".join(RECALLS)
        raise SystemExit(f"Unknown recall {recall_id!r}. Known recalls: {known}") from None


def _int_env(name: str, default: int | None) -> int | None:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    worker_id: str = "worker-1"
    step_delay_ms: int = 0
    crash_at_seq: int | None = None
    max_attempts: int = 3
    urls: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        carrier = os.getenv("CARRIER_URL", "http://localhost:8003")
        return cls(
            worker_id=os.getenv("WORKER_ID", "worker-1"),
            step_delay_ms=_int_env("STEP_DELAY_MS", 0) or 0,
            crash_at_seq=_int_env("CRASH_AT_SEQ", None),
            max_attempts=_int_env("MAX_ATTEMPTS", 3) or 3,
            urls={
                "warehouse": os.getenv("WAREHOUSE_URL", "http://localhost:8001"),
                "orders": os.getenv("ORDERS_URL", "http://localhost:8002"),
                "carrier": carrier,
                "notify": carrier,  # notifications live on the carrier mock
            },
        )

    @property
    def actor(self) -> str:
        return f"tracer-agent:{self.worker_id}"
