"""Create (or update) the dashboard's voice agent in ElevenLabs. Run once.

    python -m scripts.setup_voice_agent             # needs ELEVENLABS_API_KEY in .env
    python -m scripts.setup_voice_agent --dry-run   # print what would be sent, call nothing

First run creates the tools and the agent and prints ELEVENLABS_AGENT_ID: put it in
.env and restart the live log. Run again after editing PROMPT or TOOLS: with
ELEVENLABS_AGENT_ID set, it updates the same agent and reuses tools by name.

The tools are *client tools*: ElevenLabs only knows their name and parameters. The
code runs in the dashboard page (web/voice.js), so no public URL or tunnel is
needed. ask_tracer goes through the console's chat (POST /chat); the lookups are
read-only routes in web/voice.py.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE = os.getenv("ELEVENLABS_API_BASE", "https://api.elevenlabs.io")
AGENT_NAME = "Tracer voice (Northfield supplier portal)"

PROMPT = """\
You are the voice of Tracer in the supplier portal of Northfield Foods. Tracer is an AI \
agent at Northfield's logistics provider that runs a product recall end to end: it locks \
the affected lot in the warehouse, cancels open orders, brings shipments back, and notifies \
each affected customer exactly once. The person speaking works at Northfield, the supplier.

You are only Tracer's ears and mouth. You do not decide or know anything yourself.

1. For anything about a recall, call ask_tracer with the supplier's words, unchanged: \
reporting a problem ("we found peanuts in the granola bars"), answering Tracer's questions, \
approving or cancelling a plan, pausing or resuming, or asking how it is going or how it went. \
Then say Tracer's reply as it is, without adding, removing or softening anything. Never \
approve, cancel, pause or resume on your own: only pass on what the supplier actually said.
2. For a question about one specific unit, order, shipment or customer, use the lookup tool \
for it and answer in one or two short sentences from what it returns.
3. Never state a status, number, lot or ID from memory. If a tool returns an error or \
"found": false, say so plainly. Never guess.
4. Speak plain English in short sentences. No lists, no markdown. If you did not catch an \
ID or a lot number, ask the supplier to repeat it.
"""

FIRST_MESSAGE = "This is Tracer. Tell me what happened, or ask me about a recall."


def _param(description: str) -> dict:
    return {"type": "string", "description": description}


# (name, description, parameters, required, seconds to wait for the browser's answer)
TOOLS = [
    ("ask_tracer",
     "Pass what the supplier said to Tracer and get Tracer's reply to say back. Use it for "
     "everything about a recall: reporting a problem, approving, cancelling, pausing, "
     "resuming, and questions about progress or the outcome.",
     {"message": _param("What the supplier said, word for word.")}, ["message"], 45),
    ("lookup_warehouse_unit", "Read one unit from the warehouse system: lot, status, hold.",
     {"unit_id": _param("Unit ID, e.g. UNIT-0007")}, ["unit_id"], 10),
    ("lookup_order", "Read one order from the order system: status, customer, lot.",
     {"order_id": _param("Order ID, e.g. ORD-1003")}, ["order_id"], 10),
    ("lookup_shipment", "Read one shipment from the carrier: status, pickup time, intercept.",
     {"shipment_id": _param("Shipment tracking ID, e.g. TRK-5003")}, ["shipment_id"], 10),
    ("check_customer_notices",
     "Count the recall notices one customer has received for this recall (should be exactly 1).",
     {"customer_id": _param("Customer ID, e.g. CUST-118")}, ["customer_id"], 10),
]


def tool_config(name: str, description: str, props: dict, required: list[str],
                timeout_s: int = 10) -> dict:
    return {
        "type": "client",
        "name": name,
        "description": description,
        "expects_response": True,      # wait for the browser's answer and give it to the LLM
        "response_timeout_secs": timeout_s,
        "parameters": {"type": "object", "properties": props, "required": required},
    }


def agent_config(tool_ids: list[str]) -> dict:
    return {
        "name": AGENT_NAME,
        "conversation_config": {
            "agent": {
                "first_message": FIRST_MESSAGE,
                "language": "en",
                "prompt": {"prompt": PROMPT, "tool_ids": tool_ids},
            },
        },
    }


class ElevenLabs:
    def __init__(self, api_key: str, client: httpx.Client | None = None):
        self.http = client or httpx.Client(base_url=BASE, timeout=30)
        self.http.headers["xi-api-key"] = api_key

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        r = self.http.request(method, path, json=body)
        if r.status_code >= 400:
            raise SystemExit(f"ElevenLabs {method} {path} → {r.status_code}: {r.text[:500]}")
        return r.json() if r.content else {}

    def existing_tools(self) -> dict[str, str]:
        """name → id for tools already in the workspace (so re-runs don't make duplicates)."""
        try:
            data = self._call("GET", "/v1/convai/tools")
        except SystemExit:
            return {}
        rows = data.get("tools", data) if isinstance(data, dict) else data
        return {t["tool_config"]["name"]: t["id"] for t in rows or []
                if isinstance(t, dict) and "id" in t and "tool_config" in t}

    def upsert_tool(self, config: dict, existing: dict[str, str]) -> str:
        if config["name"] in existing:
            tool_id = existing[config["name"]]
            self._call("PATCH", f"/v1/convai/tools/{tool_id}", {"tool_config": config})
            return tool_id
        return self._call("POST", "/v1/convai/tools", {"tool_config": config})["id"]

    def upsert_agent(self, config: dict, agent_id: str | None) -> str:
        if agent_id:
            self._call("PATCH", f"/v1/convai/agents/{agent_id}", config)
            return agent_id
        return self._call("POST", "/v1/convai/agents/create", config)["agent_id"]


def setup(api: ElevenLabs, agent_id: str | None) -> str:
    existing = api.existing_tools()
    tool_ids = []
    for spec in TOOLS:
        config = tool_config(*spec)
        tool_ids.append(api.upsert_tool(config, existing))
        print(f"  tool {config['name']:<24} {tool_ids[-1]}")
    return api.upsert_agent(agent_config(tool_ids), agent_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the payloads, call nothing")
    args = parser.parse_args(argv)

    if args.dry_run:
        print(json.dumps({"tools": [tool_config(*t) for t in TOOLS],
                          "agent": agent_config(["<tool ids>"])}, indent=2))
        return 0
    key = os.getenv("ELEVENLABS_API_KEY")
    if not key:
        print("ELEVENLABS_API_KEY is not set in .env", file=sys.stderr)
        return 2
    agent_id = os.getenv("ELEVENLABS_AGENT_ID") or None
    print(("Updating agent " + agent_id) if agent_id else "Creating the voice agent")
    new_id = setup(ElevenLabs(key), agent_id)
    if agent_id:
        print(f"Updated {new_id}. The change is live, no restart needed.")
    else:
        print(f"\nDone. Add this line to .env, then restart the live log:\nELEVENLABS_AGENT_ID={new_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
