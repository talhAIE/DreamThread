"""Enforce revision source hashes and exact evidence spans in PostgreSQL."""

from alembic import op
from sqlalchemy import text

revision = "0002_revision_guard"
down_revision = "0001_core"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    connection.execute(text("""
        CREATE FUNCTION app.validate_evidence() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, app AS $$
        DECLARE source_text text; source_hash text;
        BEGIN
          SELECT r.text, r.content_hash INTO source_text, source_hash
            FROM app.dream_revisions r
            WHERE r.user_id = NEW.user_id AND r.dream_id = NEW.dream_id
              AND r.id = NEW.revision_id;
          IF source_text IS NULL OR NEW.content_hash <> source_hash
             OR NEW."end" > char_length(source_text)
             OR substring(source_text FROM NEW."start" + 1 FOR NEW."end" - NEW."start")
                <> NEW.exact_quote THEN
            RAISE EXCEPTION 'invalid source evidence' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END $$
    """))
    connection.execute(text("REVOKE ALL ON FUNCTION app.validate_evidence() FROM PUBLIC"))
    connection.execute(text("""
        CREATE TRIGGER validate_evidence BEFORE INSERT OR UPDATE ON app.evidence
        FOR EACH ROW EXECUTE FUNCTION app.validate_evidence()
    """))


def downgrade():
    op.execute("DROP TRIGGER validate_evidence ON app.evidence")
    op.execute("DROP FUNCTION app.validate_evidence()")
