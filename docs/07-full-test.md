# 07 · Full test in the live log

A by-hand pass through everything a judge will see. About 15 minutes. Run it on the laptop that will demo.

## Setup

```bash
git pull
scripts/run_demo.sh
```

Open the "live log" URL the script prints (`http://localhost:8000` by default; set `LIVE_LOG_PORT` in `.env` if 8000 is taken).

- If your browser shows a different project on `localhost:8000`, start with `LIVE_LOG_PORT=8090 scripts/run_demo.sh` and open `http://localhost:8090`.
- Keep your private `TRACER_DB` (for example `tracer_dev_a`) in `.env` for this pass, so it doesn't touch the shared database.

## Run 1: recall `RC-GB-2611` with all four disruptions

1. Press **Reset everything**, so the recalls, the mock systems, the supplier chat and memory all start empty.
2. Before starting, press **Warehouse glitch** (chapter 2) and **Carrier changes its API** (chapter 4). Both can be armed in advance.
3. Press **Start agent**.
4. During chapter 3 (cancels), press **Truck arrives early**.
5. During chapter 5 (notices), press **Crash the agent**, wait for it to show as dead, then press **Restart agent**.

| Disruption | What you should see |
|---|---|
| Warehouse glitch | The first hold fails on read-back, then verifies on a retry with the same key |
| Truck arrives early | One cancel fails with "already shipped", and Tracer stops that shipment instead |
| Carrier API change | The first intercept gets a 404, Claude proposes a fix, it verifies on one shipment and is saved to memory; the remaining intercepts pass first try |
| Crash + restart | One step is reconciled: either "already done, not resent" or resent with the same key. No customer is notified twice |

The run should end with every chapter complete and 0 duplicate notifications.

Before resetting, check it from the terminal:

```bash
python -m scripts.check_exactly_once RC-GB-2611   # expect ✅
```

## Run 2: recall `RC-OB-3107`, the memory proof

1. Press **Reset, keep learned fixes**. Do not use "Reset everything" here: the saved fix must survive.
2. Switch the recall selector to `RC-OB-3107`.
3. Press **Carrier changes its API** again, because Reset cleared it.
4. Press **Start agent**.

You should see the first intercept fail with a 404, then "fix found in memory" with no LLM call, and the comparison showing LLM calls 1 → 0.

```bash
python -m scripts.check_exactly_once RC-OB-3107   # expect ✅
python -m scripts.compare_recalls                 # only recall 2 has data after the Reset
```

## Then one integration run on the shared database

Tell your teammate first. Remove the `TRACER_DB` line from `.env`, restart `scripts/run_demo.sh`, and repeat Run 1 once. That is the database the judges will see.

## Report back

- Anything on the page that reads wrong or confusingly during a disruption
- Any step that ends `failed` and stays failed
- How long Run 1 takes at the default speed, since the 1-minute video depends on it

## Last full result (2:55 PM, live log, private database `tracer_rehearsal`, large synthetic world)

Searched 4,842 cases, 15,030 orders and 13,250 shipments for 5,547 customers. All four disruptions in Run 1.

| | `RC-GB-2611` | `RC-OB-3107` |
|---|---|---|
| Steps verified | 61/61 (60 + 1 escalation) | 24/24 |
| Warehouse glitch | caught on read-back, UNIT-0001 verified on attempt 2 | not armed |
| Truck arrives early | ORD-1010 shipped first; escalated, TRK-7010 stopped | not armed |
| Carrier API change | 404 → Claude's fix tested on TRK-7010, saved to memory | 404 → fix found in memory (score 0.93), **no LLM call** |
| Crash | killed at #48 after the notice was sent, before read-back; restart reconciled it, not resent | none |
| `prove_recall` | ✅ PROVEN, every check | ✅ PROVEN, every check |
| Exactly one notice per customer | ✅ 22/22 | ✅ 8/8 |

Run 1 took about 1 min 30 s at the default speed, plus the restart.

To rehearse without touching anyone else's run, use the `rehearsal` entry in `.claude/launch.json`: its own database (`TRACER_DB=tracer_rehearsal`, created with `python -m store.setup_db`), mocks on 8011–8013 and the live log on 8015. Scripts need the same variables:

```bash
TRACER_DB=tracer_rehearsal WAREHOUSE_URL=http://localhost:8011 ORDERS_URL=http://localhost:8012 CARRIER_URL=http://localhost:8013 python -m scripts.prove_recall RC-GB-2611
```
