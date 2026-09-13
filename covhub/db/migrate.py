"""Alembic 的程序化封装：运行时不依赖 alembic.ini，脚本目录就在包里。"""

import os

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from ..errors import ConfigError
from . import engine as db_engine

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")


def alembic_config(url):
    cfg = Config()
    cfg.set_main_option("script_location", MIGRATIONS_DIR)
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade(url, revision="head"):
    command.upgrade(alembic_config(url), revision)


def current(url):
    """当前库的版本号（没建过表时为 None）。"""
    with db_engine.configure(url).connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def head():
    return ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()


def ensure_head(url):
    """autoUpgrade 关掉时用：库结构不是最新就拒绝启动，别让旧表静默吃新字段。"""
    now, latest = current(url), head()
    if now != latest:
        raise ConfigError("数据库结构不是最新（当前 %s，应为 %s），先执行 covhub db upgrade"
                          % (now or "空库", latest))


def revision(url, message, autogenerate=True):
    command.revision(alembic_config(url), message=message, autogenerate=autogenerate)
