# Tracer

Product recall agent for the MongoDB "Agents in Context" Toronto Hackathon (Sat Nov 14, 2026), Track 3: Finish the Job. Submissions due 5:00 PM ET.

**Read [TRACER_TEAM_CONTEXT.md](TRACER_TEAM_CONTEXT.md) first.** It holds the schemas, architecture, roles, timeline, demo script and judge Q&A. If `TRACER_HANDOFF.md` exists locally (it is gitignored), read it too for setup status.

## Rules that must not be broken

- **MongoDB only.** All state, audit, memory and vector search live in MongoDB Atlas, database `tracer`. No Pinecone, Redis, Postgres, Temporal. The only outside service is the LLM.
- **Public repo.** Never commit `.env`, `.mcp.json` or any connection string or password. Secrets go in `.env` only.
- **New work only.** Everything is written today. Keep the "built today" list in the README current.
- **No banned project shapes:** no Streamlit, no basic RAG, no dashboard as the main feature. The UI is a live log only.
- All data is synthetic.

## Stack

Python + FastAPI, `pymongo`, `python-dotenv`. `db.py` reads `MONGODB_URI` from `.env`. Three mock services (warehouse, orders, carrier + notify) with chaos toggles.

The MongoDB MCP server's connection string has no default database, so always name the database `tracer` explicitly.

## Core invariants

- Step lifecycle is `planned → sent → verified / failed`. Only `verified` counts as done.
- Save the step as `sent` **before** calling the API (write-ahead intent).
- A 200 response is not proof. Mark `verified` only after reading the record back, and store the `proof`.
- After a crash, reconcile `sent` steps against the real system by idempotency key. Never replay, never skip.
- Every call carries an idempotency key, e.g. `RC-GB-2611:notify:CUST-118`.
- Step status and its audit entry are written in one transaction.
- A repair goes into `experiences` only after it passes a read-back test.

## If behind schedule

Cut in this order: judge's phone page → pause flag → TTL expiry. Never cut crash resume or read-back verification.

## Working with Zuhriddin

When explaining a software feature, also explain what it is for, so the reasoning is learned along with the code.
