"""Single MongoDB client for the whole app.

Usage:
    from store.db import client, db
    db.steps.find_one(...)

The client is created once per process and reused: MongoClient keeps a
connection pool, so creating one per request would be slow and waste connections.
"""
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

# Always explicit: the connection string may not name a database.
# TRACER_DB lets each teammate test in a private copy (e.g. TRACER_DB=tracer_dev_a, then
# `python -m store.setup_db`) so one person's reset doesn't wipe another's run.
# The demo always uses "tracer".
DB_NAME = os.environ.get("TRACER_DB", "tracer")

_uri = os.environ.get("MONGODB_URI")
if not _uri:
    raise RuntimeError("MONGODB_URI is not set. Copy .env.example to .env and fill it in.")

client = MongoClient(
    _uri,
    appname="tracer",
    serverSelectionTimeoutMS=10_000,  # fail fast with a clear error if Atlas is unreachable
    tz_aware=True,                    # datetimes come back as UTC-aware, matching what we write
)
db = client[DB_NAME]


def now() -> datetime:
    """Current time in UTC. Use this everywhere so all timestamps are comparable."""
    return datetime.now(timezone.utc)
