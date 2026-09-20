"""Engine lifecycle and explicit Alembic migration entry point.

Database operations use short synchronous sessions, matching the console's
existing store API. No transaction spans a model call or approval wait.
"""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path

from .db_engine import Database, upgrade


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the console PostgreSQL database")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("upgrade", help="Apply Alembic migrations with the console stopped")
    bootstrap = commands.add_parser("bootstrap", help="Create the first administrator offline")
    bootstrap.add_argument("--key", required=True)
    importer = commands.add_parser("import-json", help="Import into an empty database")
    importer.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    url = os.environ.get("MINICODEX_DATABASE_URL", "")
    if not url:
        parser.error("set MINICODEX_DATABASE_URL first")
    if args.command == "upgrade":
        upgrade(url)
        print("Database schema is current.")
    elif args.command == "bootstrap":
        from .db_stores import DatabaseAccounts

        password = getpass.getpass("Administrator password: ")
        if password != getpass.getpass("Repeat password: "):
            parser.error("passwords do not match")
        db = Database(url)
        try:
            accounts = DatabaseAccounts(db)
            token = accounts.issue_bootstrap_token()
            if token is None:
                parser.error("an account already exists; bootstrap is closed")
            accounts.redeem_bootstrap(token, args.key, password)
            print("Administrator created.")
        finally:
            db.close()
    else:
        from .import_json import import_json

        db = Database(url)
        try:
            print(import_json(db, args.data_dir))
        finally:
            db.close()


if __name__ == "__main__":
    main()
