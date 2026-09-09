"""partial index for in-stock variant lookups

Every catalog row asks "is anything of this product buyable?", and the in-stock
filter asks it across the whole catalog. Nothing indexed the
``status``/``stock_quantity`` half of that predicate, so it fell back to
scanning the variants table: 51,607 rows read to answer it once.

Partial, matching the EXISTS predicate exactly — only sellable variants are
worth indexing. Measured on a 60,000-variant copy: 20.9 ms to 7.2 ms, for
488 kB of index against a 12 MB table.

Small enough to build in place; product_variants is not a table that gets large
enough for the write lock to be worth CONCURRENTLY here.

Revision ID: 22b9a0739c5f
Revises: 418e42bdc908
Create Date: 2026-09-09 18:06:36.887838

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '22b9a0739c5f'
down_revision = '418e42bdc908'
branch_labels = None
depends_on = None

SELLABLE = sa.text("status = 'active' AND stock_quantity > 0")


def upgrade():
    with op.batch_alter_table('product_variants', schema=None) as batch_op:
        batch_op.create_index(
            'ix_product_variants_in_stock',
            ['product_id'],
            unique=False,
            postgresql_where=SELLABLE,
        )


def downgrade():
    with op.batch_alter_table('product_variants', schema=None) as batch_op:
        batch_op.drop_index(
            'ix_product_variants_in_stock',
            postgresql_where=SELLABLE,
        )
