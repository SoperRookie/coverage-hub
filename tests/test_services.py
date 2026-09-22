"""服务配置入库：to_dict 的形态、相对路径展开、校验与增删改查。"""
import os

import pytest

from covhub import ops
from covhub.db import repo
from covhub.errors import CovhubError, ServiceNotFound
from covhub.schemas import ServiceSpec

PULL = {"name": "order", "version": "1.4.2", "address": "10.0.0.1", "port": 6300,
        "includes": ["com.example.order.*"], "classfiles": ["./data/order/artifacts/1.4.2"]}


def test_to_dict_omits_none_scalars_and_keeps_lists(db):
    repo.add_service(ServiceSpec(**PULL).to_fields())
    svc = repo.get_service("order")
    # 没配的标量键不能出现：agent_opts 用 svc.get("bindAddress", "0.0.0.0") 取默认
    assert "bindAddress" not in svc and "dumpRetry" not in svc and "classDumpDir" not in svc
    assert svc["excludes"] == [] and svc["sourcefiles"] == [] and svc["reportExcludes"] == []
    assert svc["version"] == "1.4.2" and svc["channel"] == "pull"


def test_relative_paths_expand_against_base_dir(db, tmp_path):
    repo.add_service(ServiceSpec(**PULL).to_fields())
    raw = repo.get_service("order")
    assert raw["classfiles"] == ["./data/order/artifacts/1.4.2"]      # 库里存原文
    expanded = repo.list_services({"baseDir": str(tmp_path)})[0]
    assert expanded["classfiles"] == [os.path.normpath(str(tmp_path / "data/order/artifacts/1.4.2"))]


def test_version_is_coerced_to_str():
    assert ServiceSpec(name="a", channel="push", version=1.4).version == "1.4"


def test_pull_requires_endpoint_and_unknown_keys_rejected():
    with pytest.raises(Exception):
        ServiceSpec(name="a", channel="pull")
    with pytest.raises(Exception):
        ServiceSpec(name="a", channel="push", foo=1)
    with pytest.raises(Exception):
        ServiceSpec(name="bad name", channel="push")


def test_crud_roundtrip(db):
    cfg = {"baseDir": "/hub"}
    ops.service_add(cfg, dict(PULL))
    with pytest.raises(CovhubError):
        ops.service_add(cfg, dict(PULL))                      # 重名
    ops.service_update(cfg, "order", {"version": "1.4.3", "classfiles": ["./x"]})
    assert repo.get_service("order")["version"] == "1.4.3"
    with pytest.raises(CovhubError):
        ops.service_update(cfg, "order", {"address": None})   # 改完不再是合法 pull 配置
    with pytest.raises(CovhubError):
        ops.service_update(cfg, "order", {})
    ops.service_replace(cfg, "order", {"channel": "push", "includes": ["a.*"]})
    svc = repo.get_service("order")
    assert svc["channel"] == "push" and "address" not in svc and svc["classfiles"] == []
    ops.service_remove(cfg, "order")
    with pytest.raises(ServiceNotFound):
        repo.get_service("order")


def test_retarget_updates_db_and_keeps_raw_paths(db):
    fields = ServiceSpec(**PULL).to_fields()
    repo.add_service(fields)
    cfg = {"baseDir": "/hub", "services": [fields]}
    out = ops.retarget(cfg, "order", version="2.0", classfiles=["./data/order/artifacts/2.0"])
    assert out == {"version": "2.0", "classfiles": ["./data/order/artifacts/2.0"]}
    with pytest.raises(ServiceNotFound):
        ops.retarget(cfg, "nosuch", version="1")


def test_upsert_is_idempotent(db):
    fields = ServiceSpec(**PULL).to_fields()
    assert repo.upsert_service(fields) == "added"
    assert repo.upsert_service(dict(fields, version="9")) == "skipped"
    assert repo.get_service("order")["version"] == "1.4.2"
    assert repo.upsert_service(dict(fields, version="9"), overwrite=True) == "updated"
    assert repo.get_service("order")["version"] == "9"


def test_export_then_import_into_another_db(db, tmp_path):
    """开发库调完切生产库，配置不会自己长出来 —— export / import 是搬配置的通道。"""
    cfg = {"baseDir": str(tmp_path)}
    ops.project_add(cfg, {"name": "shop", "title": "商城"})
    ops.service_add(cfg, dict(PULL, project="shop"))
    ops.service_add(cfg, {"name": "pay", "channel": "push", "includes": ["com.pay.*"]})

    data = ops.export_config(cfg)
    assert [p["name"] for p in data["projects"]] == ["shop"]
    assert data["services"][0]["project"] == "shop"
    assert "id" not in data["services"][0]
    # 存原文：相对路径不展开，另一台 hub 的 baseDir 不一样也照样成立
    assert data["services"][0]["classfiles"] == ["./data/order/artifacts/1.4.2"]
    assert "bindAddress" not in data["services"][0]      # None 的标量不出现，导回去也不会填默认值

    import yaml
    src = tmp_path / "export.yaml"
    src.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    # 模拟切到另一个库：清空后从文件导回
    ops.service_remove(cfg, "order")
    ops.service_remove(cfg, "pay")
    ops.project_remove(cfg, "shop")
    out = ops.import_legacy(cfg, str(src), with_state=False)
    assert out["projects"] == {"shop": "added"}
    assert out["services"] == {"order": "added", "pay": "added"}
    assert repo.get_project("shop")["title"] == "商城"
    assert repo.get_service("order")["project"] == "shop"
    assert repo.get_service("order")["classfiles"] == ["./data/order/artifacts/1.4.2"]
    assert ops.export_config(cfg) == data

    # 幂等；--overwrite 才覆盖
    again = ops.import_legacy(cfg, str(src), with_state=False)
    assert again["projects"] == {"shop": "skipped"} and set(again["services"].values()) == {"skipped"}


def test_import_creates_project_the_service_refers_to(db, tmp_path):
    """旧 targets.yaml 没有 projects 段；服务指着库里没有的项目时按名字建出来，别卡在 project add 上。"""
    import yaml
    src = tmp_path / "svc.yaml"
    src.write_text(yaml.safe_dump({"services": [dict(PULL, project="shop")]}), encoding="utf-8")
    cfg = {"baseDir": str(tmp_path)}
    dry = ops.import_legacy(cfg, str(src), dry_run=True, with_state=False)
    assert dry["services"] == {"order": "added"}
    assert repo.list_projects() == []                       # 试运行不建
    out = ops.import_legacy(cfg, str(src), with_state=False)
    assert out["services"] == {"order": "added"}
    assert [p["name"] for p in repo.list_projects()] == ["shop"]
    assert repo.get_service("order")["project"] == "shop"
