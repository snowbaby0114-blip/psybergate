"""Link every dispense to the formulary rule that governed it.

A data migration in three phases, all inside this migration's single transaction:

1. expand   - add dispenses.rule_id as a nullable column, so rows that already exist stay valid;
2. backfill - point each existing dispense at the rule in force on its business date;
3. contract - once no dispense is left without a rule, make rule_id NOT NULL and add the
              composite foreign key (rule_id, medicine_id) -> formulary_rules (id, medicine_id),
              which also guarantees that the rule belongs to the dispensed medicine.

The business date is computed by the SQL itself, as
(dispensed_at AT TIME ZONE 'Africa/Johannesburg')::date. A migration must not import application
code (a later change to app.domain.timeutil would silently change what an already released
migration does), and PostgreSQL's time zone database implements the same zone. The conversion
is not cosmetic: 2025-05-31T22:00:00Z is already 2025-06-01 in Johannesburg and belongs to the
rule that starts on 2025-06-01, although its UTC date is still 31 May. Casting the timestamptz
to date would use the session time zone instead (UTC on our connections): the previous rule here,
and yet another outcome under any other setting. The explicit conversion makes the result
independent of who runs the migration and how their session is configured.

Rule periods are half-open, [effective_from, effective_to) with a NULL end meaning open-ended,
and are matched with the same daterange expression as the exclusion constraint from 0001. That
constraint guarantees at most one rule per medicine and date, so the backfill is deterministic.

A dispense with no governing rule (a gap in the rule history, or a date before the first rule)
is not guessed at: the migration aborts naming the offending ids and, because the whole migration
is one transaction, the database stays at revision 0002.

The downgrade drops the foreign key and the column. Upgrading again recomputes the same links,
provided the periods of rules that governed recorded dispenses were not altered, which the
supersede service forbids.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BACKFILL_RULE_ID = """
UPDATE dispenses AS d
SET rule_id = r.id
FROM formulary_rules AS r
WHERE r.medicine_id = d.medicine_id
  AND daterange(r.effective_from, r.effective_to, '[)')
      @> (d.dispensed_at AT TIME ZONE 'Africa/Johannesburg')::date
"""

# Plain concatenation instead of format()/RAISE placeholders: no percent signs to escape, so the
# statement means the same when executed and when rendered by `alembic upgrade --sql`.
REQUIRE_A_RULE_FOR_EVERY_DISPENSE = """
DO $$
DECLARE
    missing bigint;
    sample text;
BEGIN
    SELECT count(*) INTO missing FROM dispenses WHERE rule_id IS NULL;
    IF missing > 0 THEN
        SELECT string_agg(id::text, ', ' ORDER BY id) INTO sample
        FROM (SELECT id FROM dispenses WHERE rule_id IS NULL ORDER BY id LIMIT 5) AS first_missing;
        RAISE EXCEPTION USING
            MESSAGE = missing || ' dispense(s) have no formulary rule in force on their '
                || 'Africa/Johannesburg business date',
            DETAIL = 'First offending dispense ids: ' || sample,
            HINT = 'Add the missing rule period(s) or correct these dispenses, '
                || 'then run the migration again.';
    END IF;
END
$$
"""


def upgrade() -> None:
    op.add_column("dispenses", sa.Column("rule_id", sa.BigInteger(), nullable=True))
    op.execute(BACKFILL_RULE_ID)
    op.execute(REQUIRE_A_RULE_FOR_EVERY_DISPENSE)
    op.alter_column("dispenses", "rule_id", nullable=False)
    op.create_foreign_key(
        "fk_dispenses_rule_id_medicine_id",
        "dispenses",
        "formulary_rules",
        ["rule_id", "medicine_id"],
        ["id", "medicine_id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_dispenses_rule_id_medicine_id", "dispenses", type_="foreignkey")
    op.drop_column("dispenses", "rule_id")
