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
