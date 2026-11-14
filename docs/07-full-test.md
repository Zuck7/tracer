# 07 · Full test in the live log

A by-hand pass through everything a judge will see. About 15 minutes. Run it on the laptop that will demo.

## Setup

```bash
git pull
scripts/run_demo.sh
```

Open the "live log" URL the script prints (`http://localhost:8000` by default).

- If your browser shows a different project on `localhost:8000`, start with `LIVE_LOG_PORT=8090 scripts/run_demo.sh` and open `http://localhost:8090`.
- Keep your private `TRACER_DB` (for example `tracer_dev_a`) in `.env` for this pass, so it doesn't touch the shared database.

## Run 1: recall `RC-GB-2611` with all four disruptions

1. Press **Reset and forget learned fixes**, so memory starts empty.
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

1. Press **Reset**. Do not use "Reset and forget learned fixes" here: the saved fix must survive.
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

## Last command-line result (1:33 PM, private database)

| | `RC-GB-2611` | `RC-OB-3107` |
|---|---|---|
| Steps verified | 60/60 | 24/24 |
| Failed attempts | 3 | 1 |
| LLM repair calls | 1 | 0 |
| Fixes found in memory | 0 | 1 |
| Reconciled after crash | 1 | 0 |
| Exactly one notice per customer | ✅ 22/22 | ✅ 8/8 |

"Truck arrives early" was not part of that run. It has not been tested end to end yet.
