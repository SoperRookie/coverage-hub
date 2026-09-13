"""测试用的数据库：默认每个用例一个干净的 SQLite 文件，表结构走真实的迁移脚本。

设了 COVHUB_TEST_DATABASE_URL 就改用那个库（MySQL / PostgreSQL），每个用例前
降到 base 再升到 head 清空 —— 跨库规则（String 长度、JSON 列默认值……）只有在真库上
跑过才算数。
"""
import os

import pytest

from covhub.db import engine, migrate

EXTERNAL = os.environ.get("COVHUB_TEST_DATABASE_URL")


@pytest.fixture
def db(tmp_path):
    if EXTERNAL:
        engine.dispose()
        migrate.downgrade(EXTERNAL, "base")
        migrate.upgrade(EXTERNAL)
        url = EXTERNAL
    else:
        url = "sqlite:///" + str(tmp_path / "t.db").replace("\\", "/")
        migrate.upgrade(url)
    engine.configure(url)
    yield url
    engine.dispose()


@pytest.fixture
def db_url_for_app(tmp_path):
    """给 TestClient 用：返回该写进配置文件的 URL（外部库先清空）。"""
    if EXTERNAL:
        # 降到 base 再升到 head 清空。app 里 prepare_database 每个进程只检查一次结构，
        # 所以这里必须自己升回 head，不能指望它
        engine.dispose()
        migrate.downgrade(EXTERNAL, "base")
        migrate.upgrade(EXTERNAL)
        return EXTERNAL
    return "sqlite:///" + str(tmp_path / "t.db").replace("\\", "/")
