# Walkthrough

Everything Tracer does, triggered by hand in the live log. About 10 minutes.

## Start

```bash
scripts/run_demo.sh
```

Open the URL it prints (`http://localhost:8000` by default; set `LIVE_LOG_PORT` in `.env` to use another port).

## Recall 1: `RC-GB-2611` with every disruption

1. Press **Reset everything**, so recalls, the mock systems, the supplier chat and memory all start empty.
2. Before starting, press **Warehouse glitch** (chapter 2) and **Carrier changes its API** (chapter 4). Both can be armed in advance.
3. Press **Start agent**.
4. During chapter 3 (cancels), press **Truck arrives early**.
5. During chapter 5 (notices), press **Crash the agent**, wait for it to show as stopped, then press **Restart agent**.

| Disruption | What you should see |
|---|---|
| Warehouse glitch | The first hold fails on read-back, then verifies on a retry with the same key |
| Truck arrives early | One cancel fails with "already shipped", and Tracer intercepts that shipment instead |
| Carrier API change | The first intercept gets a 404, Claude proposes a fix, it verifies on one shipment and is saved to memory; the remaining intercepts pass first try |
| Crash + restart | One step is reconciled: either "already done, not resent" or resent with the same key. No customer is notified twice |

The run ends with every chapter complete. Click any step to see its full trace, or open the list of steps that hit a problem. Then check it from the terminal:

```bash
python -m scripts.prove_recall RC-GB-2611         # expect ✅ PROVEN
python -m scripts.check_exactly_once RC-GB-2611   # expect ✅
```

## Recall 2: `RC-OB-3107`, memory

1. Press **Reset, keep learned fixes**. Not "Reset everything": the saved fix must survive.
2. Switch the recall selector to `RC-OB-3107`.
3. Press **Carrier changes its API** again (the reset cleared it).
4. Press **Start agent**.

The first intercept fails with a 404, then "fix found in memory" with no LLM call, and the comparison shows LLM calls 1 → 0.

```bash
python -m scripts.check_exactly_once RC-OB-3107
python -m scripts.compare_recalls
```

## From the supplier's side

Open `/console` and describe a problem in plain language, for example "customers with a peanut allergy reacted to our oat granola bars". Tracer searches the supplier's lot records, proposes the recall with its reasons, and changes nothing until you approve. Steps that need a person show up here to resolve, and the report is here when the recall finishes.

## Last full result

Searched 4,842 cases, 15,030 orders and 13,250 shipments for 5,547 customers, all four disruptions in recall 1.

| | `RC-GB-2611` | `RC-OB-3107` |
|---|---|---|
| Steps verified | 61/61 (60 + 1 escalation) | 24/24 |
| Warehouse glitch | caught on read-back, UNIT-0001 verified on attempt 2 | not armed |
| Truck arrives early | ORD-1010 shipped first; escalated, TRK-7010 stopped | not armed |
| Carrier API change | 404 → Claude's fix tested on TRK-7010, saved to memory | 404 → fix found in memory (score 0.93), **no LLM call** |
| Crash | killed at #48 after the notice was sent, before read-back; restart reconciled it, not resent | none |
| `prove_recall` | ✅ PROVEN, every check | ✅ PROVEN, every check |
| Exactly one notice per customer | ✅ 22/22 | ✅ 8/8 |

Recall 1 takes about 1 min 30 s at the default speed, plus the restart.
