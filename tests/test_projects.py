"""项目维度：CRUD、服务归属、迁移不能把子表清空。"""
import pytest
from sqlalchemy import text

from covhub import ops
from covhub.db import engine, migrate, repo
from covhub.errors import CovhubError, ServiceNotFound
from covhub.schemas import ServiceSpec

PULL = {"name": "order", "address": "10.0.0.1", "port": 6300}


def test_project_crud_and_service_binding(db):
    cfg = {"baseDir": "/hub"}
    ops.project_add(cfg, {"name": "shop", "title": "商城"})
    with pytest.raises(CovhubError):
        ops.project_add(cfg, {"name": "shop"})
    with pytest.raises(CovhubError):
        ops.project_add(cfg, {"name": "bad name"})

    repo.add_service(ServiceSpec(**PULL, project="shop").to_fields())
    assert repo.get_service("order")["project"] == "shop"
    assert repo.get_project("shop")["services"] == ["order"]
    assert [s["name"] for s in repo.list_services(project="shop")] == ["order"]

    with pytest.raises(CovhubError):
        ops.service_update(cfg, "order", {"project": "nosuch"})
    ops.service_update(cfg, "order", {"project": None})           # 显式 null = 解绑
    assert "project" not in repo.get_service("order")

    ops.service_update(cfg, "order", {"project": "shop"})
    ops.service_replace(cfg, "order", {"channel": "push"})        # PUT 没给 project → 清空
    assert "project" not in repo.get_service("order")

    ops.service_update(cfg, "order", {"project": "shop"})
    ops.project_remove(cfg, "shop")
    assert "project" not in repo.get_service("order")             # 服务还在，只是未分组
    with pytest.raises(ServiceNotFound):
        repo.get_project("shop")


def test_reserved_service_names_rejected():
    for bad in ("api", "assets", "index.html", "favicon.ico"):
        with pytest.raises(Exception):
            ServiceSpec(name=bad, channel="push")


def test_migration_0002_keeps_child_rows(tmp_path):
    """给 services 加外键在 SQLite 上要重建表；连接若开了 foreign_keys，DROP 会级联清空子表。"""
    url = "sqlite:///" + str(tmp_path / "m.db").replace("\\", "/")
    migrate.upgrade(url, "0001")
    engine.configure(url)
    # 旧结构下 ORM 模型已经带 project_id 列，用裸 SQL 造数据
    with engine.get_engine().begin() as conn:
        conn.execute(text("insert into services (id, name, channel, includes, excludes, classfiles, "
                          "sourcefiles, report_excludes, created_at, updated_at) values "
                          "(1, 'order', 'pull', '[]', '[]', '[]', '[]', '[]', "
                          "'2026-09-13 10:00:00', '2026-09-13 10:00:00')"))
        conn.execute(text("insert into service_state (service_id, push_mixed, updated_at) "
                          "values (1, 0, '2026-09-13 10:00:00')"))
        conn.execute(text("insert into snapshots (service_id, at, kind, version, instruction, branch, "
                          "covered, total, classes_hit, classes_total) values "
                          "(1, '2026-09-13 10:00:00', 'dump', '1', 1.0, 1.0, 1, 2, 1, 1)"))
    engine.dispose()

    migrate.upgrade(url)
    engine.configure(url)
    with engine.get_engine().connect() as conn:
        assert conn.execute(text("select count(*) from service_state")).scalar() == 1
        assert conn.execute(text("select count(*) from snapshots")).scalar() == 1
    assert repo.latest("order")["version"] == "1"
    engine.dispose()


def test_service_must_leave_project_before_joining_another(db):
    cfg = {"baseDir": "/hub"}
    ops.project_add(cfg, {"name": "a"})
    ops.project_add(cfg, {"name": "b"})
    repo.add_service(ServiceSpec(**PULL, project="a").to_fields())
    with pytest.raises(CovhubError):
        ops.service_update(cfg, "order", {"project": "b"})     # 直接 A → B 不行
    ops.service_update(cfg, "order", {"project": None})        # 先移出
    ops.service_update(cfg, "order", {"project": "b"})         # 再归入
    assert repo.get_service("order")["project"] == "b"
    ops.service_update(cfg, "order", {"project": "b"})         # 归入自己所在的项目是幂等的
