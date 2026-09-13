"""测试用的临时数据库：每个用例一个干净的 SQLite 文件，表结构走真实的迁移脚本。"""
import pytest

from covhub.db import engine, migrate


@pytest.fixture
def db(tmp_path):
    url = "sqlite:///" + str(tmp_path / "t.db").replace("\\", "/")
    migrate.upgrade(url)
    engine.configure(url)
    yield url
    engine.dispose()
