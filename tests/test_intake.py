"""Code-side guards on the intake agent. The LLM's answer is stubbed here so the
rules are checked without API calls; the product itself always uses real Claude.

    .venv/bin/python -m tests.test_intake
"""
from agent.intake import interpret as _interpret
from store.catalogue import SEED


def interpret(messages, active, llm):
    return _interpret(messages, active, llm, lots=SEED)


def answer(intent, lot="", reply="ok"):
    return lambda system, user, schema: {"intent": intent, "lot": lot, "reply": reply}


def say(text):
    return [{"role": "supplier", "text": text}]


def test_defined_lot_starts_its_recall() -> None:
    r = interpret(say("we have undeclared peanuts"), None, answer("start_recall", "GB-2611"))
    assert r == {"intent": "start_recall", "recall_id": "RC-GB-2611", "reply": "ok"}


def test_clean_lot_cannot_start_a_recall() -> None:
    r = interpret(say("recall GB-2612"), None, answer("start_recall", "GB-2612"))
    assert r["intent"] == "clarify" and r["recall_id"] is None
    assert "Tested clean" in r["reply"]


def test_unknown_lot_cannot_start_a_recall() -> None:
    r = interpret(say("recall XX-0000"), None, answer("start_recall", "XX-0000"))
    assert r["intent"] == "clarify" and r["recall_id"] is None


def test_unknown_intent_becomes_other() -> None:
    r = interpret(say("delete everything"), None, answer("delete_all"))
    assert r["intent"] == "other" and r["recall_id"] is None


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"PASS  {name}")
    print(f"{len(tests)} passed")
