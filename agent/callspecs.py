"""CallSpecs: each API call described as data instead of code.

A CallSpec says how to perform one action (method, path, body) and how to
prove it worked (which record to read back and what it must say). Because it
is plain data, the LLM can propose a new one when an API changes, we can test
it on one step, and we can store it in `experiences` once it is proven.

Placeholders in {braces} are filled from the step: target_id, recall_id, lot,
reason, message, idempotency_key.

Two optional fields go beyond the shape in docs/contracts.md §6:
  readback_match    {"dotted.path": "{placeholder}"} extra fields that must
                    match, e.g. the hold must belong to *this* recall
  readback_id_field dotted path of the id stored in proof.read_back_id
  readback_query    query params for the read-back GET
and readback_field "$count" means "the read-back returns a list; check its
length" (used for "exactly one notification").
"""

from __future__ import annotations

import copy
import re
from typing import Any

# action → (system, verb used in the idempotency key)
ACTIONS: dict[str, tuple[str, str]] = {
    "hold_inventory": ("warehouse", "hold"),
    "cancel_order": ("orders", "cancel"),
    "intercept_shipment": ("carrier", "intercept"),
    "notify_customer": ("notify", "notify"),
}

COUNT_FIELD = "$count"

DEFAULT_SPECS: dict[str, dict[str, Any]] = {
    "hold_inventory": {
        "method": "POST",
        "path": "/inventory/{target_id}/hold",
        "body": {"recall_id": "{recall_id}", "reason": "{reason}"},
        "readback_path": "/inventory/{target_id}",
        "readback_field": "status",
        "readback_expect": ["hold"],
        "readback_match": {"hold.recall_id": "{recall_id}"},
        "readback_id_field": "hold.hold_id",
    },
    "cancel_order": {
        "method": "POST",
        "path": "/orders/{target_id}/cancel",
        "body": {"recall_id": "{recall_id}", "reason": "{reason}"},
        "readback_path": "/orders/{target_id}",
        "readback_field": "status",
        "readback_expect": ["cancelled"],
        "readback_id_field": "order_id",
    },
    "intercept_shipment": {
        "method": "POST",
        "path": "/intercepts",
        "body": {"tracking_no": "{target_id}", "recall_id": "{recall_id}", "reason": "{reason}"},
        "readback_path": "/shipments/{target_id}",
        "readback_field": "status",
        "readback_expect": ["intercept_requested"],
    },
    "notify_customer": {
        "method": "POST",
        "path": "/notifications",
        "body": {
            "customer_id": "{target_id}",
            "recall_id": "{recall_id}",
            "channel": "email",
            "message": "{message}",
        },
        "readback_path": "/notifications",
        "readback_query": {"idempotency_key": "{idempotency_key}"},
        "readback_field": COUNT_FIELD,
        "readback_expect": [1],
        "readback_id_field": "notification_id",
    },
}

_REQUIRED = ("method", "path", "readback_path", "readback_field", "readback_expect")
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def default_specs() -> dict[str, dict[str, Any]]:
    """A fresh copy, so a repair in one run never leaks into the defaults."""
    return copy.deepcopy(DEFAULT_SPECS)


def validate(spec: Any) -> list[str]:
    """Shape check for a CallSpec (used on LLM output before we trust it)."""
    if not isinstance(spec, dict):
        return ["CallSpec must be a JSON object"]
    errors = [f"missing field {key!r}" for key in _REQUIRED if key not in spec]
    if str(spec.get("method", "")).upper() not in _METHODS:
        errors.append(f"method must be one of {sorted(_METHODS)}")
    for key in ("path", "readback_path"):
        if key in spec and not (isinstance(spec[key], str) and spec[key].startswith("/")):
            errors.append(f"{key} must be a string starting with '/'")
    if "body" in spec and spec["body"] is not None and not isinstance(spec["body"], dict):
        errors.append("body must be an object or null")
    expect = spec.get("readback_expect")
    if "readback_expect" in spec and not (isinstance(expect, list) and expect):
        errors.append("readback_expect must be a non-empty list")
    for key in ("readback_match", "readback_query"):
        if key in spec and not isinstance(spec[key], dict):
            errors.append(f"{key} must be an object")
    return errors


def fill(template: Any, values: dict[str, Any]) -> Any:
    """Replace {placeholders} recursively. Unknown placeholders are left as-is."""
    if isinstance(template, str):
        whole = _PLACEHOLDER.fullmatch(template)
        if whole and whole.group(1) in values:
            return values[whole.group(1)]  # keeps non-string types intact
        return _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), template)
    if isinstance(template, dict):
        return {key: fill(value, values) for key, value in template.items()}
    if isinstance(template, list):
        return [fill(value, values) for value in template]
    return template


def get_path(obj: Any, dotted: str) -> Any:
    """Read "a.b.0.c" out of nested dicts/lists. Missing → None."""
    current = obj
    for part in dotted.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def idempotency_key(recall_id: str, action: str, target_id: str) -> str:
    """{recall_id}:{verb}:{target_id}. Never changes across retries or restarts."""
    verb = ACTIONS[action][1]
    return f"{recall_id}:{verb}:{target_id}"
