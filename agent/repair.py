"""Repair: when a system changes its API, find a working call shape again.

    the old call gets a 4xx (e.g. carrier v2: POST /intercepts → 404)
      1. memory   Atlas Vector Search over proven fixes (store.memory.find_fix).
                  A hit means no LLM call at all.
      2. LLM      otherwise Claude reads the failing call, the error and the
                  system's current /api-spec, and proposes one CallSpec (JSON
                  forced by a schema).
      3. guard    the proposal may change the endpoint and field names only:
                  same target ({target_id}), same proof (readback_expect).
      4. trial    the agent tries it on THIS step only and reads back.
      5. save     only a fix that passed read-back goes to memory.

Why this order: memory is instant and free, and it already passed a read-back
once. The LLM is the fallback, and its answer is never trusted until the real
system shows the effect. A wrong guess costs one failed step, not the recall.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from agent.callspecs import validate
from agent.clients import CallResult, Systems

MODEL = os.getenv("REPAIR_MODEL", "claude-opus-5-5")
REPAIRABLE_STATUS = {400, 404, 405, 410, 422}  # "the call shape is wrong", not a business conflict

# Structured output: the API guarantees the reply matches this schema. Every
# object needs additionalProperties false, so the body is a list of name/value pairs.
_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["method", "path", "body_fields", "readback_path", "readback_field",
                 "readback_expect", "explanation"],
    "properties": {
        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
        "path": {"type": "string"},
        "body_fields": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "value"],
            "properties": {"name": {"type": "string"}, "value": {"type": "string"}}}},
        "readback_path": {"type": "string"},
        "readback_field": {"type": "string"},
        "readback_expect": {"type": "array", "items": {"type": "string"}},
        "explanation": {"type": "string"},
    },
}

_SYSTEM_PROMPT = """You repair one API call for a product-recall agent.
A business system changed its API, so a call that used to work now fails.
Using the system's CURRENT API description, write the call that performs the
SAME action on the SAME record, plus how to read the record back to prove it worked.

Rules:
- Use these placeholders exactly; the agent fills them in: {target_id} (the record
  being acted on, e.g. the shipment id), {recall_id}, {reason}, {lot}.
- {target_id} must appear in the path or in a body value, and in readback_path.
- Do not change what the action does or which record it targets. Only the
  endpoint, method and field names may change.
- readback_field is the field of the read-back record to check; readback_expect
  lists the values that prove success (keep the same meaning as the old call).
- body_fields lists the JSON body as name/value pairs (values may be placeholders).
- explanation: REQUIRED, never empty. One short plain-English sentence a recall
  coordinator would understand, naming what changed, e.g. "The carrier moved
  intercepts to POST /shipments/{id}/return and renamed tracking_no to shipment_ref.\""""


@dataclass
class Proposal:
    spec: dict[str, Any]
    source: str  # "memory" or "llm"
    situation: str
    explanation: str = ""
    score: float | None = None


class Memory(Protocol):
    def situation_text(self, system: str, action: str, method: str, path: str, status: int,
                       detail: str) -> str: ...
    def find_fix(self, system: str, situation: str) -> dict[str, Any] | None: ...
    def save_fix(self, system: str, action: str, situation: str, fix: dict[str, Any],
                 source_recall: str) -> bool: ...


ProposeFn = Callable[[dict[str, Any], CallResult, Any], dict[str, Any]]


def guard(proposal: dict[str, Any], old: dict[str, Any]) -> list[str]:
    """Shape check plus the safety rule: same target, same proof."""
    errors = validate(proposal)
    if errors:
        return errors
    target_in_call = "{target_id}" in proposal["path"] or "{target_id}" in json.dumps(
        proposal.get("body") or {})
    if not target_in_call:
        errors.append("the call must target {target_id}")
    if "{target_id}" not in proposal["readback_path"]:
        errors.append("the read-back must look at {target_id}")
    if [str(v) for v in proposal["readback_expect"]] != [str(v) for v in old["readback_expect"]]:
        errors.append(f"readback_expect must stay {old['readback_expect']} (same proof as before)")
    return errors


class Repairer:
    def __init__(self, systems: Systems, memory: Memory | None,
                 propose_with_llm: ProposeFn | None = None) -> None:
        self.systems = systems
        self.memory = memory
        self.propose_with_llm = propose_with_llm or claude_propose
        self.llm_calls = 0

    def situation(self, system: str, action: str, spec: dict[str, Any], result: CallResult) -> str:
        detail = result.body.get("detail", "") if isinstance(result.body, dict) else ""
        # Path template, not the filled path, so every failing step gives the same text.
        args = (system, action, result.method, spec["path"], result.status or 0, str(detail))
        if self.memory:
            return self.memory.situation_text(*args)
        return f"{system} {action}: {args[2]} {args[3]} returned {args[4]} {args[5]}".strip()

    def from_memory(self, system: str, situation: str) -> Proposal | None:
        if not self.memory:
            return None
        hit = self.memory.find_fix(system, situation)
        if not hit:
            return None
        return Proposal(dict(hit["fix"]), "memory", situation, score=float(hit.get("score", 0)))

    def from_llm(self, system: str, spec: dict[str, Any], result: CallResult,
                 situation: str) -> Proposal:
        api_spec = self.systems.get_json(system, "/api-spec")
        self.llm_calls += 1
        raw = self.propose_with_llm(spec, result, api_spec)
        explanation = str(raw.pop("explanation", ""))
        return Proposal(_to_callspec(raw, spec), "llm", situation, explanation=explanation)

    def remember(self, proposal: Proposal, system: str, action: str, recall_id: str) -> bool:
        if proposal.source != "llm" or not self.memory:
            return False
        return self.memory.save_fix(system, action, proposal.situation, proposal.spec, recall_id)


def _to_callspec(raw: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
    """LLM reply → CallSpec. Keeps the old spec's optional read-back extras (id field,
    query) only when the read-back path is unchanged."""
    spec = {
        "method": raw.get("method"),
        "path": raw.get("path"),
        "body": {f["name"]: f["value"] for f in raw.get("body_fields", [])} or None,
        "readback_path": raw.get("readback_path"),
        "readback_field": raw.get("readback_field"),
        "readback_expect": raw.get("readback_expect"),
    }
    if spec["readback_path"] == old.get("readback_path"):
        for key in ("readback_id_field", "readback_query"):
            if key in old:
                spec[key] = old[key]
    return spec


def claude_propose(spec: dict[str, Any], result: CallResult, api_spec: Any) -> dict[str, Any]:
    """One Claude call. Returns the schema-shaped dict (not yet a CallSpec)."""
    import anthropic  # imported here so tests and the in-memory path don't need a key

    client = anthropic.Anthropic(api_key=os.environ["LLM_API_KEY"])
    user = (
        "The old call (CallSpec):\n" + json.dumps(spec, indent=2) +
        f"\n\nWhat came back: HTTP {result.status} " + json.dumps(result.body) +
        "\n\nThe system's current API description:\n" + json.dumps(api_spec, indent=2)
    )
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": "low",
                       "format": {"type": "json_schema", "schema": _SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("the model declined to propose a repair")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("the repair answer was cut off (max_tokens)")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)
