"""项目维度、单测报告、diff、快照增量列。

Revision ID: 0002

给 services 加外键在 SQLite 上走 batch 模式：建临时表 → 拷数据 → DROP services → 改名。
**这条连接上 foreign_keys 必须是 OFF**（migrations/env.py 自建引擎、不挂 db/engine.py 的
PRAGMA 监听器），否则 DROP services 会级联清空 service_state / snapshots / archives / breaks。
tests/test_history.py 有用例守着这个前提。
"""
from alembic import op
import sqlalchemy as sa


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('projects',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_projects')),
    sa.UniqueConstraint('name', name=op.f('uq_projects_name'))
    )
    op.create_table('diffs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('service_id', sa.Integer(), nullable=False),
    sa.Column('version', sa.String(length=100), nullable=False),
    sa.Column('base', sa.String(length=200), nullable=False),
    sa.Column('head', sa.String(length=200), nullable=True),
    sa.Column('at', sa.DateTime(), nullable=False),
    sa.Column('files', sa.Integer(), nullable=False),
    sa.Column('added_lines', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['service_id'], ['services.id'], name=op.f('fk_diffs_service_id_services'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_diffs')),
    sa.UniqueConstraint('service_id', 'version', name='uq_diffs_service_version')
    )
    op.create_table('unit_reports',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('service_id', sa.Integer(), nullable=False),
    sa.Column('version', sa.String(length=100), nullable=False),
    sa.Column('at', sa.DateTime(), nullable=False),
    sa.Column('instruction', sa.Double(), nullable=False),
    sa.Column('branch', sa.Double(), nullable=False),
    sa.Column('line', sa.Double(), nullable=False),
    sa.Column('covered', sa.BigInteger(), nullable=False),
    sa.Column('total', sa.BigInteger(), nullable=False),
    sa.Column('lines_covered', sa.Integer(), nullable=False),
    sa.Column('lines_total', sa.Integer(), nullable=False),
    sa.Column('classes_hit', sa.Integer(), nullable=False),
    sa.Column('classes_total', sa.Integer(), nullable=False),
    sa.Column('inc_covered', sa.Integer(), nullable=True),
    sa.Column('inc_total', sa.Integer(), nullable=True),
    sa.Column('inc_pct', sa.Double(), nullable=True),
    sa.Column('xml_path', sa.String(length=500), nullable=False),
    sa.ForeignKeyConstraint(['service_id'], ['services.id'], name=op.f('fk_unit_reports_service_id_services'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_unit_reports')),
    sa.UniqueConstraint('service_id', 'version', name='uq_unit_reports_service_version')
    )
    with op.batch_alter_table('service_state', schema=None) as batch_op:
        batch_op.add_column(sa.Column('online', sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column('online_at', sa.DateTime(), nullable=True))

    with op.batch_alter_table('services', schema=None) as batch_op:
        batch_op.add_column(sa.Column('project_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_services_project_id_projects'), 'projects', ['project_id'], ['id'], ondelete='SET NULL')

    with op.batch_alter_table('snapshots', schema=None) as batch_op:
        batch_op.add_column(sa.Column('inc_covered', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('inc_total', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('inc_pct', sa.Double(), nullable=True))



def downgrade():
    with op.batch_alter_table('snapshots', schema=None) as batch_op:
        batch_op.drop_column('inc_pct')
        batch_op.drop_column('inc_total')
        batch_op.drop_column('inc_covered')

    with op.batch_alter_table('services', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_services_project_id_projects'), type_='foreignkey')
        batch_op.drop_column('project_id')

    with op.batch_alter_table('service_state', schema=None) as batch_op:
        batch_op.drop_column('online_at')
        batch_op.drop_column('online')

    op.drop_table('unit_reports')
    op.drop_table('diffs')
    op.drop_table('projects')
