from alembic import context
from sqlalchemy import engine_from_config, pool

from covhub.db.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(url=config.get_main_option("sqlalchemy.url"),
                      target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    # 这里故意自建引擎、**不**挂 db/engine.py 的 SQLite PRAGMA 监听器：batch 模式改表要
    # DROP 再重建，foreign_keys=ON 会把子表级联清空（见 0002 的说明）
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}),
                                     prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        # render_as_batch：SQLite 不能 ALTER 约束，改表要走"建新表-拷数据-换名"，
        # 开发机上跑第二版迁移全靠它
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
