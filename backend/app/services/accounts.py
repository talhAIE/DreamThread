from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.auth.identity import Identity
from app.db.models import tables
from app.errors import APIError
from app.schemas.identity import Me, Preferences

ACCOUNTS, PREFS = tables["app.accounts"], tables["app.preferences"]


async def active_account(connection, user_id):
    row = (await connection.execute(
        select(ACCOUNTS).where(ACCOUNTS.c.id == user_id).with_for_update()
    )).mappings().one_or_none()
    if row is None or row["deletion_state"] != "active":
        raise APIError("account_unavailable", "Account is unavailable", 403)
    return row


class AccountService:
    def __init__(self, db):
        self.db = db

    async def ensure(self, identity: Identity):
        async with self.db.tenant_transaction(identity.user_id) as c:
            await c.execute(insert(ACCOUNTS).values(
                id=identity.user_id, is_anonymous=identity.is_anonymous,
            ).on_conflict_do_nothing(index_elements=[ACCOUNTS.c.id]))
            account = await active_account(c, identity.user_id)
            if account["is_anonymous"] != identity.is_anonymous:
                await c.execute(update(ACCOUNTS).where(ACCOUNTS.c.id == identity.user_id).values(
                    is_anonymous=identity.is_anonymous, version=ACCOUNTS.c.version + 1,
                ))
            await c.execute(insert(PREFS).values(
                user_id=identity.user_id, timezone=account["timezone"],
            ).on_conflict_do_nothing(index_elements=[PREFS.c.user_id]))

    async def read(self, identity: Identity, *, preferences=False):
        async with self.db.tenant_transaction(identity.user_id) as c:
            row = await active_account(c, identity.user_id)
            if preferences:
                row = (await c.execute(select(PREFS).where(
                    PREFS.c.user_id == identity.user_id
                ))).mappings().one()
            return self.serialize(row, preferences)

    @staticmethod
    def serialize(row, preferences):
        if not preferences:
            return Me(**{key: row[key] for key in Me.model_fields})
        display = row["display_settings"]
        return Preferences(
            auto_connections=row["auto_connections"],
            require_memory_confirmation=row["require_memory_confirmation"],
            timezone=row["timezone"], theme=display.get("theme", "dark"),
            reduced_motion=display.get("reduced_motion", False),
            version=row["version"], updated_at=row["updated_at"],
        )

    async def patch(self, identity, payload, *, preferences=False):
        async with self.db.tenant_transaction(identity.user_id) as c:
            account = await active_account(c, identity.user_id)  # consistent lock order
            pref = (await c.execute(select(PREFS).where(
                PREFS.c.user_id == identity.user_id
            ).with_for_update())).mappings().one()
            row, table = (pref, PREFS) if preferences else (account, ACCOUNTS)
            if payload.expected_version != row["version"]:
                raise APIError("version_conflict", "Resource changed; reload and retry", 409)
            changes = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
            values = {k: v for k, v in changes.items() if k not in {"theme", "reduced_motion"}}
            if preferences:
                display = dict(pref["display_settings"])
                display.update({k: v for k, v in changes.items()
                                if k in {"theme", "reduced_motion"}})
                values["display_settings"] = display
            values["version"] = table.c.version + 1
            key = table.c.user_id if preferences else table.c.id
            result = (await c.execute(update(table).where(key == identity.user_id)
                                     .values(**values).returning(table))).mappings().one()
            if "timezone" in changes and changes["timezone"] != account["timezone"]:
                other = ACCOUNTS if preferences else PREFS
                other_key = other.c.id if preferences else other.c.user_id
                await c.execute(update(other).where(other_key == identity.user_id).values(
                    timezone=changes["timezone"], version=other.c.version + 1,
                ))
            return self.serialize(result, preferences)
