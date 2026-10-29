"""Schema invariants independent of external infrastructure."""

from sqlalchemy import UniqueConstraint

from app.db.models import tables


def test_all_tenant_tables_have_owner_keys_indexes_and_tenant_parent_references():
    for table in tables.values():
        if "user_id" not in table.c:
            continue
        unique_sets = {
            tuple(c.name for c in constraint.columns)
            for constraint in table.constraints if isinstance(constraint, UniqueConstraint)
        }
        assert ("user_id", "id") in unique_sets, table.name
        assert any(index.columns[0].name == "user_id" for index in table.indexes), table.name
        for constraint in table.foreign_key_constraints:
            if constraint.referred_table.name == "accounts":
                assert [c.name for c in constraint.columns] == ["user_id"]
            else:
                assert "user_id" in constraint.columns, (table.name, constraint.name)


def test_current_revision_key_contains_dream_identity():
    constraint = next(c for c in tables["app.dreams"].foreign_key_constraints
                      if c.name == "fk_dream_current_revision")
    assert tuple(c.name for c in constraint.columns) == ("user_id", "id", "current_revision_id")
    assert constraint.deferrable
