"""FastAPI 控制面：鉴权三来源、静态目录门禁、错误形态、服务 CRUD。

不起被测 JVM，所以采集类接口只验到「目标不可达 → 409」这一层。
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from covhub.api.app import create_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def hub(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "svc" / "current").mkdir(parents=True)
    (data / "svc" / "current" / "jacoco.xml").write_text("<report/>", encoding="utf-8")
    cfg = {
        "jacocoAgent": os.path.join(ROOT, "lib", "jacocoagent.jar"),
        "jacocoCli": os.path.join(ROOT, "lib", "jacococli.jar"),
        "dataDir": str(data),
        "database": {"url": "sqlite:///" + str(tmp_path / "t.db").replace("\\", "/")},
        "serve": {"token": "secret"},
    }
    cfg_path = tmp_path / "covhub.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.delenv("COVHUB_TOKEN", raising=False)
    monkeypatch.delenv("COVHUB_DATABASE_URL", raising=False)
    app = create_app(str(cfg_path))
    with TestClient(app, base_url="http://hub") as client:
        client.headers.pop("Authorization", None)
        yield client


H = {"X-Covhub-Token": "secret"}
PULL = {"name": "svc", "address": "127.0.0.1", "port": 6301, "includes": ["a.*"]}


def test_health_is_open_and_pretty(hub):
    r = hub.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.text.startswith("{\n  ")           # indent=2


def test_token_three_sources(hub):
    assert hub.get("/api/status").status_code == 401
    assert hub.get("/api/status", headers=H).status_code == 200
    assert hub.get("/api/status?token=secret").status_code == 200
    assert hub.get("/api/status", cookies={"covhub_token": "secret"}).status_code == 200
    assert hub.get("/api/status", headers={"X-Covhub-Token": "nope"}).status_code == 401


def test_status_keeps_grep_friendly_format(hub):
    hub.post("/api/services", headers=H, json=PULL)
    r = hub.get("/api/status?service=svc", headers=H)
    assert '"online": false' in r.text          # covhub-client.sh wait-online 靠这个空格


def test_static_gate_and_cookie_grant(hub):
    assert hub.get("/").status_code == 401
    assert hub.get("/svc/current/jacoco.xml").status_code == 401
    r = hub.get("/?token=secret", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/"
    assert "covhub_token=secret" in r.headers["set-cookie"]
    assert "HttpOnly" in r.headers["set-cookie"]
    assert hub.get("/svc/current/", cookies={"covhub_token": "secret"}).status_code == 200
    r = hub.get("/svc/current/jacoco.xml", headers=H)
    assert r.status_code == 200 and r.text == "<report/>"
    # /api/* 上带 ?token= 只放行，不跳转
    assert hub.get("/api/health?token=secret", follow_redirects=False).status_code == 200


def test_static_rejects_traversal_and_unknown_api(hub):
    assert hub.get("/svc/%2e%2e/%2e%2e/covhub.json", headers=H).status_code == 404
    assert hub.get("/../covhub.json", headers=H).status_code == 404
    r = hub.get("/api/nope", headers=H)
    assert r.status_code == 404 and r.json() == {"ok": False, "error": "未知接口"}
    # 目录不带斜杠先补上，报告页的相对链接靠它
    r = hub.get("/svc/current", headers=H, follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"].endswith("/svc/current/")


def test_write_route_errors_have_uniform_shape(hub):
    r = hub.post("/api/dump", headers=H)
    assert r.status_code == 400 and r.json() == {"ok": False, "error": "缺少参数 service"}
    r = hub.post("/api/dump?service=nosuch", headers=H)
    assert r.status_code == 404 and r.json()["ok"] is False
    r = hub.get("/api/diagnose", headers=H)                 # 缺必填 query → 400 而不是 422
    assert r.status_code == 400 and r.json()["ok"] is False
    hub.post("/api/services", headers=H, json=PULL)
    r = hub.post("/api/dump?service=svc", headers=H)          # 目标不可达 → 409
    assert r.status_code == 409
    body = r.json()
    assert body["ok"] is False and body["service"] == "svc" and "连不上" in body["log"]
    r = hub.post("/api/predeploy?service=svc&version=1&allowMissing=1", headers=H)
    assert r.status_code == 200 and r.json()["ok"] is True


def test_post_params_from_query_json_or_form(hub):
    hub.post("/api/services", headers=H, json=PULL)
    assert hub.post("/api/dump", headers=H, json={"service": "svc"}).status_code == 409
    assert hub.post("/api/dump", headers=H, data={"service": "svc"}).status_code == 409


def test_agent_opts_text_and_jar(hub):
    hub.post("/api/services", headers=H, json=PULL)
    r = hub.get("/api/agent-opts?service=svc&format=text", headers=H)
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text.startswith("-javaagent:") and "sessionid=svc" in r.text
    r = hub.get("/api/agent.jar", headers=H)
    assert r.status_code == 200 and r.headers["content-type"] == "application/java-archive"


def test_services_crud(hub):
    r = hub.post("/api/services", headers=H, json=PULL)
    assert r.status_code == 201 and r.json()["service"]["name"] == "svc"
    assert hub.post("/api/services", headers=H, json=PULL).status_code == 409
    r = hub.post("/api/services", headers=H, json={"name": "x", "channel": "pull"})
    assert r.status_code == 400 and "address" in r.json()["error"]
    r = hub.post("/api/services", headers=H, json={"name": "x", "channel": "push", "bogus": 1})
    assert r.status_code == 400
    r = hub.patch("/api/services/svc", headers=H, json={"version": "2", "classfiles": ["./c"]})
    assert r.status_code == 200 and r.json()["service"]["classfiles"] == ["./c"]
    assert hub.patch("/api/services/svc", headers=H, json={"address": None}).status_code == 409
    r = hub.put("/api/services/svc", headers=H, json={"channel": "push", "includes": ["b.*"]})
    assert r.status_code == 200 and "address" not in r.json()["service"]
    assert hub.get("/api/services", headers=H).json()["services"][0]["channel"] == "push"
    assert hub.delete("/api/services/svc", headers=H).status_code == 200
    assert hub.get("/api/services/svc", headers=H).status_code == 404


def test_cors_only_on_openapi(hub):
    r = hub.get("/api/openapi.json")
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
    assert "/api/predeploy" in r.json()["paths"]
    assert "access-control-allow-origin" not in hub.get("/api/health").headers
    assert "access-control-allow-origin" not in hub.get("/api/status", headers=H).headers


DIFF = ("--- a/src/main/java/probe/Main.java\n+++ b/src/main/java/probe/Main.java\n"
        "@@ -4,0 +5,1 @@\n+    static void tick() { }\n")
XML = ('<?xml version="1.0"?><report name="u"><package name="probe"><sourcefile name="Main.java">'
       '<line nr="5" mi="0" ci="2" mb="0" cb="0"/><line nr="9" mi="1" ci="0" mb="0" cb="0"/></sourcefile>'
       '<counter type="INSTRUCTION" missed="1" covered="2"/><counter type="LINE" missed="1" covered="1"/>'
       '<counter type="CLASS" missed="0" covered="1"/></package>'
       '<counter type="INSTRUCTION" missed="1" covered="2"/><counter type="LINE" missed="1" covered="1"/>'
       '<counter type="CLASS" missed="0" covered="1"/></report>')


def test_build_inputs_diff_then_unit_xml(hub):
    hub.post("/api/services", headers=H, json=dict(PULL, version="2.0"))
    # 正文用 --data-binary 的形态：form 类型的 Content-Type，不能被当表单解析
    r = hub.post("/api/diff?service=svc&version=2.0&base=v1",
                 headers={**H, "Content-Type": "application/x-www-form-urlencoded"}, content=DIFF.encode())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["diff"]["addedLines"] == 1 and body["matchesCurrentVersion"] is True
    assert body["runtime"] is None                       # 还没有快照

    r = hub.post("/api/unit-coverage?service=svc&version=2.0",
                 headers={**H, "Content-Type": "application/x-www-form-urlencoded"}, content=XML.encode())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["report"]["line"] == 50.0
    assert body["incremental"] == {"covered": 1, "total": 1, "pct": 100.0, "unmatched": 0, "ambiguous": 0}

    assert hub.post("/api/unit-coverage?service=svc&version=2.0", headers=H, content=b"<html/>").status_code == 400
    assert hub.post("/api/diff?service=svc&version=2.0&base=v1", headers=H, content=b"garbage").status_code == 400
    assert hub.post("/api/diff?service=svc&version=../x&base=v1", headers=H, content=DIFF.encode()).status_code == 400
    assert hub.post("/api/diff?service=svc&version=2.0&base=v1", headers=H).status_code == 400   # 空正文
    r = hub.post("/api/recompute?service=svc&version=2.0", headers=H)
    assert r.status_code == 200 and r.json()["unit"]["pct"] == 100.0


def test_overview_and_detail_shapes(hub):
    hub.post("/api/projects", headers=H, json={"name": "shop"})
    hub.post("/api/services", headers=H, json=dict(PULL, version="2.0", project="shop"))
    hub.post("/api/services", headers=H, json={"name": "lonely", "channel": "push"})
    hub.post("/api/diff?service=svc&version=2.0&base=v1", headers=H, content=DIFF.encode())
    hub.post("/api/unit-coverage?service=svc&version=2.0", headers=H, content=XML.encode())

    r = hub.get("/api/overview", headers=H)
    assert r.status_code == 200
    o = r.json()
    assert [p["name"] for p in o["projects"]] == ["shop"]
    row = o["projects"][0]["services"][0]
    assert row["name"] == "svc" and row["unknown"] is True and row["online"] is None   # 还没轮询过
    assert row["runtime"] is None and row["unit"]["incremental"]["pct"] == 100.0
    assert row["diff"]["addedLines"] == 1
    assert [s["name"] for s in o["unassigned"]] == ["lonely"]
    assert o["counts"]["services"] == 2 and "averageInstruction" not in o["projects"][0]["counts"]

    r = hub.get("/api/services/svc/detail", headers=H)
    assert r.status_code == 200
    d = r.json()
    assert d["unit"]["latest"]["line"] == 50.0
    assert d["unit"]["incremental"]["files"][0]["path"] == "src/main/java/probe/Main.java"
    assert d["runtime"]["latest"] is None and d["runtime"]["xmlUrl"] == "/svc/current/jacoco.xml"
    assert d["config"]["includes"] == ["a.*"]
    assert hub.get("/api/services/nosuch/detail", headers=H).status_code == 404
    r = hub.get("/api/services/svc/versions", headers=H)
    assert r.status_code == 200 and r.json()["latest"] is None
