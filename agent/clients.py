"""HTTP access to the business systems: run a CallSpec, then read the record back.

`send` returns what the system *said*. `read_back` returns what the system
*shows*. Only the read-back can mark a step verified: a 200 means "received",
not "done" (the drop-writes chaos returns 200 and changes nothing).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from agent.callspecs import COUNT_FIELD, fill, get_path

RETRYABLE_STATUS = {408, 425, 429}


@dataclass
class CallResult:
    method: str
    path: str
    status: int | None  # None = no response (network error, timeout)
    body: Any = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def retryable(self) -> bool:
        """Worth sending again with the same key. 4xx means the call itself is wrong."""
        return self.status is None or self.status >= 500 or self.status in RETRYABLE_STATUS

    def describe(self) -> str:
        if self.status is None:
            return f"{self.method} {self.path} → no response ({self.error})"
        detail = self.body.get("detail") if isinstance(self.body, dict) else None
        return f"{self.method} {self.path} → {self.status}" + (f" {detail}" if detail else "")


@dataclass
class ReadBack:
    verified: bool
    observed: str  # what we saw, stored in proof.observed_status
    read_back_id: str | None = None
    count: int | None = None  # only for list read-backs (notifications)
    error: str | None = None

    @property
    def duplicate(self) -> bool:
        return self.count is not None and self.count > 1


class Systems:
    """One httpx client per system. `transport` lets tests swap in fake systems."""

    def __init__(
        self,
        urls: dict[str, str],
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._clients = {
            name: httpx.Client(base_url=url, transport=transport, timeout=timeout)
            for name, url in urls.items()
        }

    def close(self) -> None:
        for client in self._clients.values():
            client.close()

    def _client(self, system: str) -> httpx.Client:
        try:
            return self._clients[system]
        except KeyError:
            raise ValueError(f"no URL configured for system {system!r}") from None

    def get_json(self, system: str, path: str, params: dict[str, Any] | None = None) -> Any:
        """Plain GET for scoping (list endpoints). Raises on HTTP errors."""
        response = self._client(system).get(path, params=params)
        response.raise_for_status()
        return response.json()

    def send(
        self, system: str, spec: dict[str, Any], values: dict[str, Any], idempotency_key: str
    ) -> CallResult:
        method = str(spec["method"]).upper()
        path = fill(spec["path"], values)
        body = fill(spec.get("body"), values)
        try:
            response = self._client(system).request(
                method, path, json=body, headers={"Idempotency-Key": idempotency_key}
            )
        except httpx.HTTPError as exc:
            return CallResult(method, path, None, error=f"{type(exc).__name__}: {exc}")
        return CallResult(method, path, response.status_code, _json_or_text(response))

    def read_back(self, system: str, spec: dict[str, Any], values: dict[str, Any]) -> ReadBack:
        path = fill(spec["readback_path"], values)
        params = fill(spec.get("readback_query"), values)
        try:
            response = self._client(system).get(path, params=params)
        except httpx.HTTPError as exc:
            return ReadBack(False, "read-back failed", error=f"{type(exc).__name__}: {exc}")
        if response.status_code != 200:
            return ReadBack(False, f"read-back HTTP {response.status_code}",
                            error=f"GET {path} → {response.status_code}")
        data = _json_or_text(response)
        expect = spec["readback_expect"]
        id_field = spec.get("readback_id_field")

        if spec["readback_field"] == COUNT_FIELD:
            items = data if isinstance(data, list) else []
            count = len(items)
            noun = "notification" if count == 1 else "notifications"
            read_back_id = get_path(items[0], id_field) if items and id_field else None
            return ReadBack(count in expect, f"{count} {noun}", _str(read_back_id), count=count)

        value = get_path(data, spec["readback_field"])
        verified = value in expect
        observed = str(value)
        if verified:  # the status is right; does the record belong to this recall?
            for dotted, wanted in fill(spec.get("readback_match") or {}, values).items():
                got = get_path(data, dotted)
                if got != wanted:
                    verified = False
                    observed += f", but {dotted}={got!r} (want {wanted!r})"
        read_back_id = get_path(data, id_field) if id_field else None
        return ReadBack(verified, observed, _str(read_back_id) or str(values.get("target_id")))


def _json_or_text(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _str(value: Any) -> str | None:
    return None if value is None else str(value)
