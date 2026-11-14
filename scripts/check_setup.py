"""Tracer setup check.

Run from the project root:  python scripts/check_setup.py

Checks that .env exists and is git-ignored, that MongoDB Atlas is reachable,
that the cluster supports transactions and change streams, that this user can
write to the tracer database, and that the API keys are present.
It never prints a secret.
"""

import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = "tracer"

failures = 0


def ok(msg: str) -> None:
    print(f"  OK    {msg}")


def warn(msg: str) -> None:
    print(f"  WARN  {msg}")


def fail(msg: str) -> None:
    global failures
    failures += 1
    print(f"  FAIL  {msg}")


def check_env_file() -> None:
    print("1. .env file")
    env_path = ROOT / ".env"
    if not env_path.exists():
        fail(".env not found. Run: cp .env.example .env  and fill it in.")
        return
    ok(".env exists")
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".env"], cwd=ROOT, check=False
    )
    if ignored.returncode == 0:
        ok(".env is git-ignored")
    else:
        fail(".env is NOT git-ignored. Add .env to .gitignore before any commit.")
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    if tracked.returncode == 0:
        fail(".env is tracked by git. Run: git rm --cached .env  and rotate the password.")


def check_mongo() -> None:
    print("2. MongoDB Atlas")
    uri = os.getenv("MONGODB_URI", "")
    if not uri or "<password>" in uri or "xxxxx" in uri:
        fail("MONGODB_URI is missing or still the placeholder.")
        return
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=8000)
        client.admin.command("ping")
        ok("connected")

        hello = client.admin.command("hello")
        version = client.server_info().get("version", "unknown")
        ok(f"server version {version}")
        if hello.get("setName") or hello.get("msg") == "isdbgrid":
            ok("replica set or sharded cluster: transactions and change streams work")
        else:
            fail("standalone server: transactions and change streams will not work")

        try:
            db = client.get_default_database()
        except PyMongoError:
            db = client[DEFAULT_DB]
        if db.name != DEFAULT_DB:
            warn(f"default database is '{db.name}', expected '{DEFAULT_DB}'. Check the URI path.")
        else:
            ok(f"default database is '{DEFAULT_DB}'")

        probe = db["_setup_check"]
        inserted = probe.insert_one({"probe": True})
        probe.delete_one({"_id": inserted.inserted_id})
        probe.drop()
        ok("this user can write to the database")

        names = sorted(db.list_collection_names())
        expected = ["audit_log", "control", "experiences", "steps"]
        missing = [n for n in expected if n not in names]
        if missing:
            warn(f"collections not created yet: {', '.join(missing)} (atlas/setup.py creates them)")
        else:
            ok("all four collections exist")
    except PyMongoError as exc:
        message = str(exc).replace(uri, "<MONGODB_URI>")[:300]
        fail(f"{type(exc).__name__}: {message}")
        print("        Common causes: your IP is not in Atlas Network Access,")
        print("        a wrong password, or a network that blocks port 27017.")


def check_keys() -> None:
    print("3. API keys")
    if os.getenv("LLM_API_KEY"):
        ok("LLM_API_KEY is set")
    else:
        fail("LLM_API_KEY is empty (needed for planning and API repair)")
    if os.getenv("VOYAGE_API_KEY"):
        ok("VOYAGE_API_KEY is set")
    else:
        warn("VOYAGE_API_KEY is empty (only needed if Automated Embedding is unavailable)")


def main() -> int:
    load_dotenv(ROOT / ".env")
    check_env_file()
    check_mongo()
    check_keys()
    print()
    if failures:
        print(f"{failures} check(s) failed. Fix them before building.")
        return 1
    print("Setup looks good.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
