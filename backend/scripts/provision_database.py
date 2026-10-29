"""Run once as the database administrator before Alembic, never from API startup."""

import argparse
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.db.connections import connect_args, postgres_url
from app.db.neon import neon_urls, provision_neon_logins
from app.db.provision import provision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--local-logins",
        action="store_true",
        help="Create documented development-only local login roles",
    )
    parser.add_argument(
        "--neon-logins",
        action="store_true",
        help="Provision restricted SQL logins using private Neon URLs from .env",
    )
    args = parser.parse_args()
    load_dotenv()
    url = os.environ.get("ADMIN_DATABASE_URL")
    if not url:
        raise RuntimeError("ADMIN_DATABASE_URL is required for explicit administrator provisioning")
    if args.local_logins and args.neon_logins:
        raise RuntimeError("Choose Neon setup or isolated CI login setup")
    urls = neon_urls(os.environ) if args.neon_logins else None
    url = postgres_url(url).render_as_string(hide_password=False)
    if args.local_logins and (
        make_url(url).host not in {"localhost", "127.0.0.1", "db"}
        or os.environ.get("APP_ENV", "development") not in {"development", "test"}
    ):
        raise RuntimeError("Development login provisioning is restricted to local development/test")
    engine = create_engine(url, hide_parameters=True, connect_args=connect_args(url, 30))
    try:
        with engine.begin() as connection:
            provision(connection)
            if urls:
                provision_neon_logins(connection, urls)
            if args.local_logins:
                for role, password, group in (
                    ("dreamthread_app", "local-runtime-only", "dreamthread_runtime"),
                    ("dreamthread_migrator", "local-migration-only", "dreamthread_owner"),
                    ("dreamthread_claimer", "local-queue-only", "dreamthread_queue"),
                ):
                    exists = connection.scalar(
                        text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:r)"), {"r": role}
                    )
                    if not exists:
                        # Values are fixed source constants, never user input.
                        connection.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'"))
                    connection.execute(text(f"GRANT {group} TO {role}"))
    finally:
        engine.dispose()
    print("Provisioned pgvector and NOLOGIN privilege groups.")
    print(
        "Neon restricted SQL logins ready; existing passwords were not reset."
        if urls
        else "Development logins ready."
        if args.local_logins
        else "Configure login roles separately."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # DB/DDL exceptions can contain password literals, URLs or provider detail.
        print(
            "Provisioning failed ("
            + type(error).__name__
            + "). Check private URLs, direct endpoint, "
            "administrator permissions, SQL role memberships and vector extension schema."
        )
        raise SystemExit(1) from None
