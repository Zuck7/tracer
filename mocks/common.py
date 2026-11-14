"""Shared plumbing for the mocks: idempotency, chaos toggles, reset, CORS.

Chaos is deterministic ("drop the next 2 writes"), so the demo behaves the same every time.
"""
import asyncio
import threading
import uuid
from typing import Callable

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


def short_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:4]}"


class ChaosIn(BaseModel):
    drop_next_writes: int | None = None
    api_version: str | None = None   # carrier only: "v1" | "v2"
    latency_ms: int | None = None


class MockState:
    """In-memory state for one mock service."""

    def __init__(self, name: str, seed_fn: Callable[[], dict]):
        self.name = name
        self.seed_fn = seed_fn
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.data = self.seed_fn()
        self.keys: dict[str, dict] = {}   # idempotency key -> first response
        self.chaos = {"drop_next_writes": 0, "api_version": "v1", "latency_ms": 0}

    def _take_drop(self) -> bool:
        if self.chaos["drop_next_writes"] > 0:
            self.chaos["drop_next_writes"] -= 1
            return True
        return False

    def write(self, key: str | None, apply: Callable[[], dict], fake: Callable[[], dict]) -> dict:
        """Idempotent write.

        - Same key seen before: return the first response, change nothing.
        - Chaos drop: return a normal-looking 200 but change nothing and DON'T store the key,
          so a retry with the same key applies for real.
        - Otherwise apply, store the response under the key, return it.
        """
        if not key:
            raise HTTPException(400, "Idempotency-Key header required")
        with self.lock:
            if key in self.keys:
                return self.keys[key]
            if self._take_drop():
                print(f"[{self.name}] CHAOS: dropped write {key} (returned 200, changed nothing)")
                return fake()
            response = apply()
            self.keys[key] = response
            return response

    def write_no_dedupe(self, key: str | None, apply: Callable[[], dict], fake: Callable[[], dict]) -> dict:
        """Write WITHOUT de-duplication (notifications, like real SMS/email gateways).

        Only the agent's reconcile logic stops a customer getting two notices.
        """
        if not key:
            raise HTTPException(400, "Idempotency-Key header required")
        with self.lock:
            if self._take_drop():
                print(f"[{self.name}] CHAOS: dropped write {key} (returned 200, changed nothing)")
                return fake()
            return apply()


def make_app(name: str, seed_fn: Callable[[], dict]) -> tuple[FastAPI, MockState]:
    app = FastAPI(title=f"Tracer mock: {name}")
    state = MockState(name, seed_fn)

    # The live log page calls /chaos directly from the browser.
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def latency(request, call_next):
        if state.chaos["latency_ms"]:
            await asyncio.sleep(state.chaos["latency_ms"] / 1000)
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"service": name, "ok": True}

    @app.get("/chaos")
    def get_chaos():
        return state.chaos

    @app.post("/chaos")
    def set_chaos(body: ChaosIn):
        for field, value in body.model_dump(exclude_none=True).items():
            state.chaos[field] = value
        print(f"[{name}] chaos -> {state.chaos}")
        return state.chaos

    @app.post("/reset")
    def reset():
        state.reset()
        print(f"[{name}] reset: data re-seeded, chaos cleared")
        return {"service": name, "reset": True}

    return app, state
