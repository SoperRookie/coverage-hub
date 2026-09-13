"""初始表结构：services / service_state / snapshots / archives / breaks。

Revision ID: 0001
"""
from alembic import op
import sqlalchemy as sa


revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('services',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('channel', sa.String(length=8), nullable=False),
    sa.Column('version', sa.String(length=100), nullable=True),
    sa.Column('address', sa.String(length=255), nullable=True),
    sa.Column('port', sa.Integer(), nullable=True),
    sa.Column('bind_address', sa.String(length=255), nullable=True),
    sa.Column('dump_retry', sa.Integer(), nullable=True),
    sa.Column('class_dump_dir', sa.String(length=500), nullable=True),
    sa.Column('source_encoding', sa.String(length=32), nullable=True),
    sa.Column('includes', sa.JSON(), nullable=False),
    sa.Column('excludes', sa.JSON(), nullable=False),
    sa.Column('classfiles', sa.JSON(), nullable=False),
    sa.Column('sourcefiles', sa.JSON(), nullable=False),
    sa.Column('report_excludes', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_services')),
    sa.UniqueConstraint('name', name=op.f('uq_services_name'))
    )
    op.create_table('service_state',
    sa.Column('service_id', sa.Integer(), nullable=False),
    sa.Column('session_start', sa.String(length=64), nullable=True),
    sa.Column('push_mixed', sa.Boolean(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['service_id'], ['services.id'], name=op.f('fk_service_state_service_id_services'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('service_id', name=op.f('pk_service_state'))
    )
    op.create_table('snapshots',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('service_id', sa.Integer(), nullable=False),
    sa.Column('at', sa.DateTime(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('version', sa.String(length=100), nullable=True),
    sa.Column('instruction', sa.Double(), nullable=False),
    sa.Column('branch', sa.Double(), nullable=False),
    sa.Column('covered', sa.BigInteger(), nullable=False),
    sa.Column('total', sa.BigInteger(), nullable=False),
    sa.Column('classes_hit', sa.Integer(), nullable=False),
    sa.Column('classes_total', sa.Integer(), nullable=False),
    sa.Column('reason', sa.String(length=32), nullable=True),
    sa.Column('session_start', sa.String(length=64), nullable=True),
    sa.Column('match_rate', sa.Double(), nullable=True),
    sa.ForeignKeyConstraint(['service_id'], ['services.id'], name=op.f('fk_snapshots_service_id_services'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_snapshots'))
    )
    with op.batch_alter_table('snapshots', schema=None) as batch_op:
        batch_op.create_index('ix_snapshots_service_at', ['service_id', 'at'], unique=False)

    op.create_table('archives',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('service_id', sa.Integer(), nullable=False),
    sa.Column('snapshot_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('version', sa.String(length=100), nullable=False),
    sa.Column('archive_dir', sa.String(length=255), nullable=False),
    sa.Column('sealed_at', sa.DateTime(), nullable=False),
    sa.Column('sealed_by', sa.String(length=32), nullable=False),
    sa.Column('fingerprint', sa.String(length=16), nullable=True),
    sa.Column('exec_count', sa.Integer(), nullable=False),
    sa.Column('match_rate', sa.Double(), nullable=True),
    sa.Column('health_verdict', sa.Text(), nullable=True),
    sa.Column('merged', sa.String(length=255), nullable=True),
    sa.ForeignKeyConstraint(['service_id'], ['services.id'], name=op.f('fk_archives_service_id_services'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['snapshot_id'], ['snapshots.id'], name=op.f('fk_archives_snapshot_id_snapshots'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_archives')),
    sa.UniqueConstraint('service_id', 'archive_dir', name='uq_archives_service_dir')
    )
    with op.batch_alter_table('archives', schema=None) as batch_op:
        batch_op.create_index('ix_archives_service_sealed', ['service_id', 'sealed_at'], unique=False)

    op.create_table('breaks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('service_id', sa.Integer(), nullable=False),
    sa.Column('at', sa.DateTime(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('from_session', sa.String(length=64), nullable=True),
    sa.Column('to_session', sa.String(length=64), nullable=True),
    sa.Column('sealed_as', sa.String(length=255), nullable=True),
    sa.Column('archive_id', sa.Integer(), nullable=True),
    sa.Column('instances', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['archive_id'], ['archives.id'], name=op.f('fk_breaks_archive_id_archives'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['service_id'], ['services.id'], name=op.f('fk_breaks_service_id_services'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_breaks'))
    )
    with op.batch_alter_table('breaks', schema=None) as batch_op:
        batch_op.create_index('ix_breaks_service_at', ['service_id', 'at'], unique=False)



def downgrade():
    # 直接 drop 表，别先 drop 索引：MySQL 会拿 (service_id, at) 这种复合索引给外键用，
    # 单独 drop 会报 "needed in a foreign key constraint"
    op.drop_table("breaks")
    op.drop_table("archives")
    op.drop_table("snapshots")
    op.drop_table("service_state")
    op.drop_table("services")
