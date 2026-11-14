"""Intake: the supplier talks to Tracer in plain language (typed now, voice later).

    "hey we have undeclared peanuts"
      → Claude reads the message, searches Northfield's production records and
        answers: lot GB-2611 granola bars, lab found peanut protein, not on the label
      → intent start_recall, recall_id RC-GB-2611

Two functions, both text in / JSON out, so a voice agent can call them too:

    interpret(messages, active) → {"intent", "recall_id", "reply"}
    explain(report, highlights) → plain-English outcome for the supplier

Safety, same rule as the rest of Tracer: the LLM understands and explains; code
decides. interpret can only name a recall that exists in agent.config.RECALLS, a
lot without a defined recall becomes a clarifying question, and starting a recall
only *finds* where the lot is. Nothing in any business system changes until the
supplier approves the plan.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from agent.config import RECALLS

MODEL = os.getenv("INTAKE_MODEL", "claude-opus-5-5")
INTENTS = ["start_recall", "clarify", "approve", "cancel", "pause", "resume", "status",
           "explain", "other"]

def supplier_lots() -> list[dict[str, Any]]:
    """Northfield's production records (products, lots, QA findings), read from
    MongoDB Atlas, collection supplier_lots (store/catalogue.py)."""
    from store.catalogue import list_lots  # through store/, like every other DB access
    return list_lots()


def candidate_lots(messages: list[dict[str, str]]) -> dict[str, Any]:
    """Shortlist the supplier's records for Claude to read: the lots closest in meaning to
    what they said (Atlas Vector Search over ~700 lots), plus lots with an open QA issue.
    Hundreds of lots don't belong in a prompt; eight relevant ones do."""
    from store import catalogue
    said = " ".join(m["text"] for m in messages if m["role"] == "supplier")[-1000:]
    hits, method = catalogue.search_lots(said)
    seen = {h["lot"] for h in hits}
    shortlist = hits + [lot for lot in catalogue.open_issues() if lot["lot"] not in seen]
    return {"lots": shortlist, "method": method, "searched": catalogue.count_lots(),
            "lookup": catalogue.find_lot}


_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intent", "lot", "reply"],
    "properties": {
        "intent": {"type": "string", "enum": INTENTS},
        "lot": {"type": "string"},  # lot code for start_recall, "" otherwise
        "reply": {"type": "string"},
    },
}

_SYSTEM = """You are Tracer, the recall agent that Northfield Foods' logistics provider runs
on their behalf. You talk to someone at Northfield (quality or operations). Be brief,
warm and concrete: 1 to 3 short sentences, plain English, no jargon.

You have searched Northfield's production records; the closest matches are below. Pick an intent:
- start_recall: they report a safety problem and ONE lot in the records clearly matches
  it AND that lot has a recall_id. Put the lot code in "lot". In "reply", say which lot
  and why it matches (cite the QA finding), and that you will now find where it is
  (warehouse, orders, trucks, homes) and show them the plan before changing anything.
- clarify: nothing matches, several lots match, or the matching lot has no recall_id
  (say what the records show, e.g. it tested clean or declares the allergen). Ask one
  short question.
- approve / cancel: they answer yes / no to a plan waiting for their approval.
- pause / resume: they want the running recall stopped for now / continued.
- status: they ask how it is going, or a factual question about the active recall.
  Answer ONLY from the "Active recall" facts given; never invent numbers.
- explain: they ask why something happened or for a summary of a finished recall.
  Use only the facts given.
- other: anything else; answer briefly and say what you can do.

Never say that something was done unless the facts say so. Never promise a result.
You do not decide which cases, orders or customers are affected: plain code does that
from the systems' records, and the supplier approves it."""


LLMFn = Callable[[str, str, dict], dict]


def interpret(messages: list[dict[str, str]], active: dict[str, Any] | None = None,
              llm: LLMFn | None = None,
              lots: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Understand the supplier's latest message. Code then checks the answer.
    `lots` defaults to the supplier's records in MongoDB."""
    search = None
    if lots is None:
        search = candidate_lots(messages)
        lots = search["lots"]
    header = (f"Production records: the {len(lots)} closest matches out of {search['searched']} "
              f"lots (found with {search['method']} search)" if search else "Production records")
    user = (
        header + ":\n" + json.dumps(lots, default=str, indent=1) +
        "\n\nActive recall (facts from MongoDB; null if none):\n" +
        json.dumps(active, default=str, indent=1) +
        "\n\nConversation so far (oldest first):\n" +
        "\n".join(f"{m['role'].upper()}: {m['text']}" for m in messages[-12:]) +
        "\n\nDecide the intent of the supplier's LAST message and write Tracer's reply."
    )
    raw = (llm or _claude_json)(_SYSTEM, user, _SCHEMA)
    lookup = search["lookup"] if search else (
        lambda code: next((r for r in lots if r["lot"] == code), None))
    result = _check(raw, lookup)
    if search:
        result["search"] = {"method": search["method"], "searched": search["searched"],
                            "candidates": [r["lot"] for r in lots]}
    return result


def _check(raw: dict[str, Any], lookup: Callable[[str], dict | None]) -> dict[str, Any]:
    """The LLM proposes; code disposes. Only real, defined recalls can start."""
    intent = raw.get("intent") if raw.get("intent") in INTENTS else "other"
    reply = str(raw.get("reply") or "").strip()
    result: dict[str, Any] = {"intent": intent, "recall_id": None, "reply": reply}
    if intent != "start_recall":
        return result
    lot = str(raw.get("lot") or "").strip().upper()
    record = lookup(lot) if lot else None  # checked against ALL records in MongoDB
    if not record:
        return {"intent": "clarify", "recall_id": None,
                "reply": f"I couldn't find lot {lot or '(none)'} in your production records. "
                         "Which product and lot is it?"}
    if not record["recall_id"] or record["recall_id"] not in RECALLS:
        return {"intent": "clarify", "recall_id": None,
                "reply": f"Lot {lot} ({record['product']}): {record['qa_finding']} "
                         "Is it a different lot?"}
    result["recall_id"] = record["recall_id"]
    return result


_EXPLAIN_SYSTEM = """You are Tracer. Explain the outcome of a product recall to the supplier
(Northfield Foods) in 3 to 6 short sentences of plain English: what was stopped, who was
told, anything that went wrong and how you handled it, and anything still needing a
person. Use ONLY the facts given: do not add actions (like "flagged" or "followed up")
that the facts don't state. Call warehouse units "cases". No jargon (no "step",
"read-back", "idempotency")."""


def explain(report: dict[str, Any], highlights: list[str],
            llm: Callable[[str, str], str] | None = None) -> str:
    user = ("Recall report (from MongoDB):\n" + json.dumps(report, default=str, indent=1) +
            "\n\nWhat happened along the way:\n" + "\n".join(f"- {h}" for h in highlights))
    return (llm or _claude_text)(_EXPLAIN_SYSTEM, user).strip()


# -- Claude -------------------------------------------------------------------
def _client():
    import anthropic  # imported here so tests don't need a key
    return anthropic.Anthropic(api_key=os.environ["LLM_API_KEY"])


def _create(**kwargs):
    response = _client().beta.messages.create(
        model=MODEL, max_tokens=4096, betas=["server-side-fallback-2026-07-01"],
        fallbacks="default", **kwargs)
    if response.stop_reason == "refusal":
        raise RuntimeError("the model declined this request")
    return next(b.text for b in response.content if b.type == "text")


def _claude_json(system: str, user: str, schema: dict) -> dict:
    text = _create(system=system, messages=[{"role": "user", "content": user}],
                   output_config={"effort": "low",
                                  "format": {"type": "json_schema", "schema": schema}})
    return json.loads(text)


def _claude_text(system: str, user: str) -> str:
    return _create(system=system, messages=[{"role": "user", "content": user}],
                   output_config={"effort": "low"})
