from __future__ import annotations

import os
from pathlib import Path

import psycopg

MIGRATIONS = Path("migrations")

# This script used to apply exactly one file, so a database that predates the tracking
# table has had exactly this much of the schema and nothing more. It is recorded as
# applied rather than re-run; everything after it is applied normally.
PRE_TRACKING = ("001_init.sql",)


def main() -> None:
    database_url = os.getenv(
        "DOCTASK_DATABASE_URL",
        "postgresql://doctask:doctask@localhost:5432/doctask",
    )
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename   TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        applied = {row[0] for row in conn.execute("SELECT filename FROM schema_migrations")}
        bootstrapped = conn.execute("SELECT to_regclass('public.collections')").fetchone()[0]
        if bootstrapped and not applied:
            for filename in PRE_TRACKING:
                conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)", (filename,)
                )
            applied = set(PRE_TRACKING)
            print(f"Recorded {len(PRE_TRACKING)} pre-existing migration(s)")

        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in applied:
                continue
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))
            print(f"Applied migrations/{path.name}")


if __name__ == "__main__":
    main()
