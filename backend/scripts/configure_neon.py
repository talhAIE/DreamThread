"""Generate private SQL-login URLs in .env from one Neon administrator URL. No network calls."""

import argparse
import os
import secrets
from pathlib import Path

from dotenv import dotenv_values, set_key

from app.db.connections import is_neon, postgres_url
from app.db.neon import LOGIN_GROUPS, neon_urls


def configure(path):
    values = dotenv_values(path)
    admin = postgres_url(
        os.environ.get("ADMIN_DATABASE_URL") or values.get("ADMIN_DATABASE_URL", ""),
        neon_required=True,
    )
    if "CHANGE_ME" in admin.password or "YOUR_ENDPOINT" in admin.host:
        raise ValueError("Replace the administrator placeholder first")
    if admin.username in {role for role, _ in LOGIN_GROUPS.values()}:
        raise ValueError("Use the administrator URL")
    updates = {"ADMIN_DATABASE_URL": admin.render_as_string(hide_password=False)}
    for key, (role, _) in LOGIN_GROUPS.items():
        existing = values.get(key)
        reuse = None
        if existing:
            try:
                candidate = postgres_url(existing)
                if (
                    is_neon(candidate)
                    and candidate.password
                    and "CHANGE_ME" not in candidate.password
                ):
                    if (
                        candidate.host,
                        candidate.port or 5432,
                        candidate.database,
                        candidate.username,
                    ) != (
                        admin.host,
                        admin.port or 5432,
                        admin.database,
                        role,
                    ):
                        raise ValueError("Existing Neon credentials target another database/role")
                    reuse = candidate
            except ValueError:
                # Placeholder URLs are replaced; meaningful Neon credentials are never
                # silently replaced or rotated by rerunning the generator.
                if ".neon.tech" in existing and "CHANGE_ME" not in existing:
                    raise
        target = reuse or admin.set(username=role, password=secrets.token_urlsafe(32))
        updates[key] = target.render_as_string(hide_password=False)
    neon_urls(updates)
    for key, value in updates.items():
        set_key(path, key, value, quote_mode="always")
    return len(LOGIN_GROUPS)


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        path = Path.cwd() / ".env"
        if not path.is_file():
            raise ValueError("Copy .env.example first")
        count = configure(path)
        print(f"Configured {count} private SQL-login URLs in backend/.env. No database calls made.")
        print("Next: python scripts/provision_database.py --neon-logins")
    except Exception:
        print(
            "Configuration failed. Set ADMIN_DATABASE_URL to the direct Neon administrator URL. "
            "Check existing Neon URLs before replacing them; secrets were not printed."
        )
        raise SystemExit(1) from None
