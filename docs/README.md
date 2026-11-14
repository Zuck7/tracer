# Tracer docs

This folder is the team's build book. If two docs disagree, **[03-contracts.md](03-contracts.md) wins**: it holds the API shapes and schemas that all three people code against.

| # | Doc | What it answers | Read it when |
|---|---|---|---|
| 01 | [Project idea](01-project-idea.md) | What are we building, for whom, and why does it fit Track 3? | First, 5 minutes |
| 02 | [Architecture](02-architecture.md) | How do the pieces fit? What happens on a crash, a silent failure, an API change? | Before writing any code |
| 03 | [Contracts](03-contracts.md) | Exact endpoints, JSON shapes, collections, validators, indexes, seed data | Every time you write code that talks to someone else's code |
| 04 | [Roadmap](04-roadmap.md) | What each person builds in each block, and the gate to pass before moving on | At the start of each block |
| 05 | [Dev guide](05-dev-guide.md) | Folder layout, setup, how to run everything, git rules, starter prompts for Claude Code | When setting up, and when stuck |
| 06 | [Demo and pitch](06-demo-and-pitch.md) | The 1-minute video, the 3-minute finals demo, judge Q&A, submission checklist | From 3:40 PM on |

## Reading order by role

| Role | Must read | Skim |
|---|---|---|
| **A: Agent** | 02 (all), 03 sections 1–4 and 6 | 04, 05 |
| **B: Atlas layer** | 02 (all), 03 sections 5–6 | 04, 05 |
| **C: Systems + demo** | 03 sections 1–4 and 7, 06 | 02, 04, 05 |

## The project in one paragraph

Lot **GB-2611** of granola bars contains undeclared peanuts. Tracer runs the recall across three systems (warehouse, orders, carrier + notifications): it holds every unit, cancels open orders, intercepts trucks and notifies every affected customer exactly once. Every step is saved in MongoDB Atlas before it runs and is only marked done after the agent reads the real system back. So when a write is silently dropped, the agent is killed mid-run, or the carrier changes its API, Tracer neither misses nor repeats anything, and it remembers how it fixed the API in Atlas Vector Search.

## Changing these docs

- Change a contract in 03 only after telling the other two people, because their code depends on it.
- Keep the "Built today" list in the root [README](../README.md) current; judges check it.
- Never paste a connection string, password or API key into any doc. This repo is public.
