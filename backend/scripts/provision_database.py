"""Run once as the database administrator before Alembic, never from API startup."""

import argparse
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.db.provision import provision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-logins", action="store_true",
                        help="Create documented development-only local login roles")
    args = parser.parse_args()
    load_dotenv()
    url = os.environ.get("ADMIN_DATABASE_URL")
    if not url:
        raise RuntimeError("ADMIN_DATABASE_URL is required for explicit administrator provisioning")
    if args.local_logins and (
        make_url(url).host not in {"localhost", "127.0.0.1", "db"}
        or os.environ.get("APP_ENV", "development") not in {"development", "test"}
    ):
        raise RuntimeError("Development login provisioning is restricted to local development/test")
    engine = create_engine(url, hide_parameters=True)
    try:
        with engine.begin() as connection:
            provision(connection)
            if args.local_logins:
                for role, password, group in (
                    ("dreamthread_app", "local-runtime-only", "dreamthread_runtime"),
                    ("dreamthread_migrator", "local-migration-only", "dreamthread_owner"),
                    ("dreamthread_claimer", "local-queue-only", "dreamthread_queue"),
                ):
                    exists = connection.scalar(text(
                        "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:r)"
                    ), {"r": role})
                    if not exists:
                        # Values are fixed source constants, never user input.
                        connection.execute(text(
                            f"CREATE ROLE {role} LOGIN PASSWORD '{password}'"
                        ))
                    connection.execute(text(f"GRANT {group} TO {role}"))
    finally:
        engine.dispose()
    print("Provisioned pgvector and NOLOGIN privilege groups.")
    print("Development logins ready." if args.local_logins else "Configure login roles separately.")


if __name__ == "__main__":
    main()
