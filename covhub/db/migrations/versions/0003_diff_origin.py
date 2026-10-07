"""diffs 加 origin：记录这份 diff 是流水线上传的还是 hub 比对源码生成的。

Revision ID: 0003

已有的行都是流水线上传的，补 'upload'。SQLite 上仍走 batch 模式。
"""
from alembic import op
import sqlalchemy as sa


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('diffs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('origin', sa.String(length=20), nullable=False,
                                      server_default='upload'))


def downgrade():
    with op.batch_alter_table('diffs', schema=None) as batch_op:
        batch_op.drop_column('origin')
