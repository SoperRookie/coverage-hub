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
def hub(tmp_path, monkeypatch, db_url_for_app):
    data = tmp_path / "data"
    (data / "svc" / "current").mkdir(parents=True)
    (data / "svc" / "current" / "jacoco.xml").write_text("<report/>", encoding="utf-8")
    cfg = {
        "jacocoAgent": os.path.join(ROOT, "lib", "jacocoagent.jar"),
        "jacocoCli": os.path.join(ROOT, "lib", "jacococli.jar"),
        "dataDir": str(data),
        "database": {"url": db_url_for_app},
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
# 端口挑一个本机不会有人监听的：6301 / 6399 是演示 JVM 在用的
PULL = {"name": "svc", "address": "127.0.0.1", "port": 65530, "includes": ["a.*"]}


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
    # 面板产物免令牌（配了 token 的 hub 首页得先能打开）；dataDir 照旧拦
    assert hub.get("/").status_code in (200, 404)
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
    # 带错令牌打开首页不报 401：让 SPA 自己去撞 401 再弹输入框
    assert hub.get("/?token=wrong", follow_redirects=False).status_code in (200, 404)
    # 产物文件不落到 dataDir 门禁；未知路径还是 404，不回落 index.html
    assert hub.get("/nonexistent.png", headers=H).status_code == 404


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


def test_incremental_json_keeps_source_snippets(hub, tmp_path):
    """算增量时把新增行附近的源码存进 incremental.json —— 历史版本看源码全靠它。"""
    src = tmp_path / "src" / "probe"
    src.mkdir(parents=True)
    (src / "Main.java").write_text("package probe;\n\nclass Main {\n\n    static void tick() { }\n}\n", encoding="utf-8")
    hub.post("/api/services", headers=H, json=dict(PULL, version="2.0", sourcefiles=[str(tmp_path / "src")]))
    hub.post("/api/diff?service=svc&version=2.0&base=v1", headers=H, content=DIFF.encode())
    hub.post("/api/unit-coverage?service=svc&version=2.0", headers=H, content=XML.encode())
    stored = json.loads((tmp_path / "data" / "svc" / "unit" / "2.0" / "incremental.json").read_text(encoding="utf-8"))
    entry = stored["files"]["src/main/java/probe/Main.java"]
    assert entry["snippets"] == {"2": "", "3": "class Main {", "4": "", "5": "    static void tick() { }", "6": "}"}
    # 源码目录没了也照样能看：片段优先于 sourcefiles
    (src / "Main.java").unlink()
    s = hub.get("/api/services/svc/source?file=src/main/java/probe/Main.java&kind=unit", headers=H).json()
    assert s["sourceFound"] is True and [l["nr"] for l in s["lines"]] == [2, 3, 4, 5, 6]
    assert s["lines"][3]["status"] == "covered"


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


def test_detail_and_source_can_view_archived_version(hub, tmp_path):
    """历史版本：detail?version= 切到归档的数字与明细，source 用归档里存下的源码片段。"""
    from covhub.db import repo
    hub.post("/api/projects", headers=H, json={"name": "shop"})
    hub.post("/api/services", headers=H, json=dict(PULL, version="2.0", project="shop"))
    hub.post("/api/diff?service=svc&version=2.0&base=v1", headers=H, content=DIFF.encode())
    hub.post("/api/unit-coverage?service=svc&version=2.0", headers=H, content=XML.encode())

    # 模拟一次 predeploy 结算：快照 + 归档目录（带 jacoco.xml 与 incremental.json，后者带源码片段）
    data = tmp_path / "data" / "svc"
    arch = data / "versions" / "2.0"
    (arch / "html").mkdir(parents=True)
    (arch / "html" / "index.html").write_text("<html/>", encoding="utf-8")
    (arch / "jacoco.xml").write_text(XML, encoding="utf-8")
    snap = repo.add_snapshot("svc", {"at": "2026-09-13T10:00:00", "kind": "predeploy", "version": "2.0",
                                     "instruction": 66.7, "branch": 0.0, "covered": 2, "total": 3,
                                     "classesHit": 1, "classesTotal": 1,
                                     "incCovered": 1, "incTotal": 1, "incPct": 100.0})
    repo.finish_archive("svc", snapshot_id=snap["id"], version="2.0", archive_dir="versions/2.0",
                        sealed_by="predeploy", sealed_at=snap["at"], match_rate=100.0)
    # 2.1 早期归档的 incremental.json 只有 missed：源码视图得能用归档里的 jacoco.xml + diff 现算出 added / hit
    (arch / "incremental.json").write_text(json.dumps({
        "version": "2.0", "covered": 1, "total": 1, "pct": 100.0, "unmatched": [], "ambiguous": [], "skipped": [],
        "files": {"src/main/java/probe/Main.java": {"covered": 1, "total": 1, "missed": [], "reportFile": "probe/Main.java", "group": None}}}),
        encoding="utf-8")
    r = hub.get("/api/services/svc/source?file=src/main/java/probe/Main.java&version=2.0", headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["sourceFound"] is False and [(l["nr"], l["status"]) for l in r.json()["lines"]] == [(5, "covered")]

    (arch / "incremental.json").write_text(json.dumps({
        "version": "2.0", "covered": 1, "total": 1, "pct": 100.0, "unmatched": [], "ambiguous": [], "skipped": [],
        "files": {"src/main/java/probe/Main.java": {
            "covered": 1, "total": 1, "missed": [], "hit": [5], "added": [5], "reportFile": "probe/Main.java",
            "group": None, "snippets": {"3": "class Main {", "4": "", "5": "    static void tick() { }", "6": "}"}}}}),
        encoding="utf-8")

    # 当前周期：latest 还是最后那次快照，但新增明细没有（current/ 下没 incremental.json）
    d = hub.get("/api/services/svc/detail", headers=H).json()
    assert d["viewingVersion"] is None and d["runtime"]["incremental"] is None
    assert d["runtime"]["xmlUrl"] == "/svc/current/jacoco.xml"

    r = hub.get("/api/services/svc/detail?version=2.0", headers=H)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["viewingVersion"]["dir"] == "2.0" and d["viewingVersion"]["sealedBy"] == "predeploy"
    assert d["runtime"]["latest"]["instruction"] == 66.7 and d["runtime"]["latest"]["incremental"]["pct"] == 100.0
    assert d["runtime"]["incremental"]["files"][0]["path"] == "src/main/java/probe/Main.java"
    assert d["runtime"]["reportUrl"] == "/svc/versions/2.0/html/index.html"
    assert d["runtime"]["xmlUrl"] == "/svc/versions/2.0/jacoco.xml"
    assert d["unit"]["latest"]["version"] == "2.0"                    # 单测栏跟着归档的版本号走
    assert hub.get("/api/services/svc/detail?version=9.9", headers=H).status_code == 409

    # 源码来自归档里存下的片段，不依赖 sourcefiles；上下文只到片段有的行
    r = hub.get("/api/services/svc/source?file=src/main/java/probe/Main.java&version=2.0", headers=H)
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["sourceFound"] is True and s["sourcePath"] == "incremental.json"
    assert [(l["nr"], l["status"]) for l in s["lines"]] == [(3, "context"), (4, "context"), (5, "covered"), (6, "context")]
    assert s["lines"][2]["text"] == "    static void tick() { }"
    assert s["reportUrl"].startswith("/svc/versions/2.0/html/")


def test_compare_versions(hub, tmp_path):
    """历史对比：归档 vs 当前周期，总量差与按文件差。"""
    from covhub.db import repo
    hub.post("/api/services", headers=H, json=dict(PULL, version="2.0"))
    data = tmp_path / "data" / "svc"
    arch = data / "versions" / "1.0"
    arch.mkdir(parents=True)
    old_xml = XML.replace('<line nr="5" mi="0" ci="2"', '<line nr="5" mi="2" ci="0"')   # 旧版这一行没跑到
    (arch / "jacoco.xml").write_text(old_xml, encoding="utf-8")
    (data / "current" / "jacoco.xml").write_text(XML, encoding="utf-8")
    old = repo.add_snapshot("svc", {"at": "2026-09-13T09:00:00", "kind": "predeploy", "version": "1.0",
                                    "instruction": 0.0, "branch": 0.0, "covered": 0, "total": 3,
                                    "classesHit": 0, "classesTotal": 1})
    repo.finish_archive("svc", snapshot_id=old["id"], version="1.0", archive_dir="versions/1.0",
                        sealed_by="predeploy", sealed_at=old["at"])
    repo.add_snapshot("svc", {"at": "2026-09-13T10:00:00", "kind": "dump", "version": "2.0",
                              "instruction": 66.7, "branch": 0.0, "covered": 2, "total": 3,
                              "classesHit": 1, "classesTotal": 1})

    r = hub.get("/api/services/svc/compare?a=1.0&b=current", headers=H)
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["a"]["label"] == "1.0" and c["b"]["label"] == "当前周期"
    assert c["delta"]["instruction"] == 66.7 and c["delta"]["covered"] == 2 and c["delta"]["classesHit"] == 1
    assert c["delta"]["incremental"] is None                     # 两边都没有新增覆盖
    f = c["files"][0]
    assert f["path"] == "probe/Main.java" and f["status"] == "changed"
    assert f["a"]["covered"] == 0 and f["b"]["covered"] == 2 and f["delta"] == 66.7
    assert c["counts"] == {"changed": 1, "same": 0, "added": 0, "removed": 0}
    assert hub.get("/api/services/svc/compare?a=9.9", headers=H).status_code == 409
    # 同一侧比自己：全部 same、差为 0
    c = hub.get("/api/services/svc/compare?a=current&b=current", headers=H).json()
    assert c["delta"]["instruction"] == 0 and c["files"][0]["status"] == "same"


def test_project_report(hub, tmp_path):
    from covhub.db import repo
    hub.post("/api/projects", headers=H, json={"name": "shop", "title": "商城"})
    hub.post("/api/services", headers=H, json=dict(PULL, version="2.0", project="shop"))
    hub.post("/api/unit-coverage?service=svc&version=2.0", headers=H, content=XML.encode())
    snap = repo.add_snapshot("svc", {"at": "2026-09-13T10:00:00", "kind": "predeploy", "version": "1.9",
                                     "instruction": 50.0, "branch": 0.0, "covered": 1, "total": 2,
                                     "classesHit": 1, "classesTotal": 1})
    repo.finish_archive("svc", snapshot_id=snap["id"], version="1.9", archive_dir="versions/1.9",
                        sealed_by="predeploy", sealed_at=snap["at"])

    r = hub.get("/api/projects/shop/report?days=0", headers=H)
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["title"] == "商城" and rep["since"] is None and rep["counts"]["services"] == 1
    svc = rep["services"][0]
    assert svc["name"] == "svc" and svc["versions"][0]["version"] == "1.9"
    assert svc["versions"][0]["reportUrl"] == "/svc/versions/1.9/html/index.html"
    assert svc["unitReports"][0]["line"] == 50.0
    assert "averageInstruction" not in rep                           # 不算项目平均

    # 时间范围过滤：3650 天之内包含 2026-09-13 之后的东西才行；一个很旧的归档会被滤掉
    old = repo.add_snapshot("svc", {"at": "2000-01-01T00:00:00", "kind": "predeploy", "version": "0.1",
                                    "instruction": 10.0, "branch": 0.0, "covered": 1, "total": 10,
                                    "classesHit": 1, "classesTotal": 1})
    repo.finish_archive("svc", snapshot_id=old["id"], version="0.1", archive_dir="versions/0.1",
                        sealed_by="predeploy", sealed_at=old["at"])
    assert [v["version"] for v in hub.get("/api/projects/shop/report?days=0", headers=H).json()["services"][0]["versions"]] == ["0.1", "1.9"]
    assert [v["version"] for v in hub.get("/api/projects/shop/report?days=3650", headers=H).json()["services"][0]["versions"]] == ["1.9"]
    assert hub.get("/api/projects/nosuch/report", headers=H).status_code == 404
    assert hub.get("/api/projects/__unassigned/report", headers=H).json()["services"] == []
