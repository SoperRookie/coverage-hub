"""FastAPI 控制面：鉴权三来源、静态目录门禁、错误形态、服务 CRUD。

不起被测 JVM，所以采集类接口只验到「目标不可达 → 409」这一层。
"""
import json
import os
import shutil

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
    # 没配 webDir（前后端分离的默认形态）：根路径是说明页，不是 401 JSON 也不是目录列表
    r = hub.get("/")
    assert r.status_code == 200 and "covhub" in r.text and "/docs" in r.text
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
    assert hub.get("/?token=wrong", follow_redirects=False).status_code == 200
    # 未知路径还是 404，不回落 index.html（脚本靠 404 判失败）
    assert hub.get("/nonexistent.png", headers=H).status_code == 404


def test_startup_log_says_who_serves_the_dashboard(tmp_path, monkeypatch, db_url_for_app, capsys):
    """启动日志要说清看板由谁托管 —— 三种形态各有一行，配歪了要告警。

    「打开 8900 怎么是一页说明」是分离部署后最常见的困惑，日志里说明白比让人翻文档强。
    """
    data = tmp_path / "data"
    data.mkdir()
    web = tmp_path / "dist"
    web.mkdir()

    def boot(web_dir):
        cfg = {"jacocoAgent": os.path.join(ROOT, "lib", "jacocoagent.jar"),
               "jacocoCli": os.path.join(ROOT, "lib", "jacococli.jar"),
               "dataDir": str(data), "database": {"url": db_url_for_app},
               "serve": {"token": "secret", "webDir": web_dir}}
        cfg_path = tmp_path / ("covhub-%s.json" % abs(hash(web_dir)))
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        with TestClient(create_app(str(cfg_path)), base_url="http://hub"):
            pass
        return capsys.readouterr().err

    monkeypatch.delenv("COVHUB_TOKEN", raising=False)
    monkeypatch.delenv("COVHUB_DATABASE_URL", raising=False)

    assert "由外部托管" in boot("")                      # 分离部署：本进程不发前端
    assert "没有 index.html" in boot(str(web))           # 配了却是空目录：拷贝漏了，告警
    (web / "index.html").write_text("<title>x</title>", encoding="utf-8")
    out = boot(str(web))
    assert "看板：" in out and "serve.webDir=" in out     # 自托管：直接给出地址


def test_self_hosted_web_dir(tmp_path, monkeypatch, db_url_for_app):
    """serve.webDir 配上了就照旧托管看板：产物免令牌，dataDir 仍要令牌。"""
    web = tmp_path / "dist"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><title>covhub 看板</title>", encoding="utf-8")
    (web / "assets" / "index-abc.js").write_text("console.log(1)", encoding="utf-8")
    data = tmp_path / "data"
    (data / "svc" / "current").mkdir(parents=True)
    cfg = {"jacocoAgent": os.path.join(ROOT, "lib", "jacocoagent.jar"),
           "jacocoCli": os.path.join(ROOT, "lib", "jacococli.jar"),
           "dataDir": str(data), "database": {"url": db_url_for_app},
           "serve": {"token": "secret", "webDir": str(web)}}
    cfg_path = tmp_path / "covhub.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.delenv("COVHUB_TOKEN", raising=False)
    monkeypatch.delenv("COVHUB_DATABASE_URL", raising=False)
    with TestClient(create_app(str(cfg_path)), base_url="http://hub") as client:
        r = client.get("/")
        assert r.status_code == 200 and "看板" in r.text          # 免令牌
        r = client.get("/assets/index-abc.js")
        assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
        assert client.get("/svc/current/", follow_redirects=False).status_code == 401
        # 自托管时 /docs 才给「回看板」的链接
        r = client.get("/docs")
        assert r.status_code != 200 or '<a href="/">' in r.text


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
    # 这个 hub 没配 covhubAgent：薄 agent 的下载是 404，并说清原因
    r = hub.get("/api/covhub-agent.jar", headers=H)
    assert r.status_code == 404 and "covhubAgent" in r.json()["error"]


def test_push_with_covhub_agent(tmp_path, monkeypatch, db_url_for_app):
    """配了 covhubAgent：push 服务的参数串是两个 -javaagent，薄 agent 能从 hub 下载。"""
    cfg = {"jacocoAgent": os.path.join(ROOT, "lib", "jacocoagent.jar"),
           "covhubAgent": os.path.join(ROOT, "lib", "covhub-agent.jar"),
           "jacocoCli": os.path.join(ROOT, "lib", "jacococli.jar"),
           "dataDir": str(tmp_path / "data"), "database": {"url": db_url_for_app},
           "serve": {"token": "secret"},
           "collect": {"advertiseAddress": "covhub.internal"}}
    cfg_path = tmp_path / "covhub.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.delenv("COVHUB_TOKEN", raising=False)
    monkeypatch.delenv("COVHUB_DATABASE_URL", raising=False)
    with TestClient(create_app(str(cfg_path)), base_url="http://hub") as client:
        assert client.get("/api/covhub-agent.jar").status_code == 401      # 和别的接口一样要令牌
        r = client.get("/api/covhub-agent.jar", headers=H)
        assert r.status_code == 200 and r.headers["content-type"] == "application/java-archive"
        assert r.content[:2] == b"PK"
        client.post("/api/services", headers=H, json={"name": "svc", "channel": "push"})
        r = client.get("/api/agent-opts?service=svc&format=text", headers=H)
        jacoco, thin = r.text.strip().split(" ")
        assert "output=none" in jacoco and "sessionid=svc" in jacoco
        assert thin.endswith("covhub-agent.jar=address=covhub.internal,port=6400,idle=900")
        # pull 服务不受影响：仍是一个 -javaagent
        client.post("/api/services", headers=H, json=dict(PULL, name="pulled"))
        r = client.get("/api/agent-opts?service=pulled&format=text", headers=H)
        assert " " not in r.text.strip() and "output=tcpserver" in r.text


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


def test_export_matches_import_shape(hub):
    hub.post("/api/projects", headers=H, json={"name": "shop", "title": "商城"})
    hub.post("/api/services", headers=H, json=dict(PULL, project="shop"))
    assert hub.get("/api/export").status_code == 401
    r = hub.get("/api/export", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["projects"] == [{"name": "shop", "title": "商城", "description": None}]
    assert body["services"][0]["project"] == "shop" and "id" not in body["services"][0]


def test_docs_page_is_open_and_self_hosted(hub):
    """/docs 不要令牌，Swagger UI 的资源从包里出，不引 CDN；spec 带令牌的 securityScheme。"""
    from covhub.api.docs import SWAGGER_DIR
    r = hub.get("/docs")
    if not (SWAGGER_DIR / "swagger-ui-bundle.js").is_file():
        assert r.status_code == 503
    else:
        assert r.status_code == 200 and "http://" not in r.text and "https://" not in r.text
        assert "./swagger/swagger-ui-bundle.js" in r.text and "spec: {" in r.text
        assert "/api/predeploy" in r.text and "</script>" in r.text
        assert hub.get("/swagger/swagger-ui.css").status_code == 200        # 免令牌
        assert hub.get("/swagger/package.json").status_code == 404          # 只发白名单里那几个
        assert '<a href="/">' not in r.text      # 没配 webDir，hub 的根不是看板
    # spec 只内嵌在 /docs 里，不再单独暴露；令牌三来源是 spec 里的 securitySchemes，Authorize 能直接用
    assert hub.get("/api/openapi.json").status_code == 404
    spec = hub.app.openapi()
    assert spec["components"]["securitySchemes"]["tokenHeader"]["name"] == "X-Covhub-Token"
    assert {"tokenHeader": []} in spec["paths"]["/api/dump"]["post"]["security"]
    assert "security" not in spec["paths"]["/api/health"]["get"]


def test_login_exchanges_token_for_cookie(hub):
    """分离部署下看板的登录入口：hub 收不到前端的 /?token=，只能靠这条。"""
    assert hub.post("/api/login").status_code == 401
    assert hub.post("/api/login", headers={"X-Covhub-Token": "wrong"}).status_code == 401
    r = hub.post("/api/login", headers=H)
    assert r.status_code == 200 and r.json() == {"ok": True, "tokenRequired": True}
    assert "covhub_token=secret" in r.headers["set-cookie"]
    assert "HttpOnly" in r.headers["set-cookie"]
    # 拿到的 Cookie 对 /api/* 和报告目录都好使
    jar = {"covhub_token": "secret"}
    assert hub.get("/api/status", cookies=jar).status_code == 200
    assert hub.get("/svc/current/jacoco.xml", cookies=jar).status_code == 200


def test_no_cors_anywhere(hub):
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


def _src_tar(files):
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, text in files.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


MAIN_SRC = "package probe;\n\nclass Main {\n\n    static void tick() { }\n}\n"


def test_upload_sources_feeds_full_source_view_for_old_versions(hub, tmp_path):
    """按版本传上来的源码：服务 retarget 到新版本之后，旧版本的新增代码仍能看整个文件。"""
    hub.post("/api/services", headers=H, json=dict(PULL, version="2.0"))
    hub.post("/api/diff?service=svc&version=2.0&base=v1", headers=H, content=DIFF.encode())
    hub.post("/api/unit-coverage?service=svc&version=2.0", headers=H, content=XML.encode())
    blob = _src_tar({"src/main/java/probe/Main.java": MAIN_SRC,
                     "src/main/resources/application.yml": "password: x\n"})
    r = hub.post("/api/upload-sources?service=svc&version=2.0",
                 headers={**H, "Content-Type": "application/x-www-form-urlencoded"}, content=blob)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sources"]["files"] == 1 and body["sources"]["roots"] == ["src/main/java"]
    assert body["matchesCurrentVersion"] is True and body["unit"]["pct"] == 100.0
    assert not (tmp_path / "data" / "svc" / "sources" / "2.0" / "src" / "main" / "resources").exists()
    # 有 diff 时顺手补上了片段
    stored = json.loads((tmp_path / "data" / "svc" / "unit" / "2.0" / "incremental.json").read_text(encoding="utf-8"))
    assert stored["files"]["src/main/java/probe/Main.java"]["snippets"]["5"] == "    static void tick() { }"

    # 发了新版本：旧版本的源码视图照样按 2.0 的源码走，还能给全文
    hub.post("/api/retarget?service=svc&version=3.0", headers=H)
    s = hub.get("/api/services/svc/source?file=src/main/java/probe/Main.java&kind=unit", headers=H).json()
    assert s["sourcePath"] == "sources/2.0" and s["sourceVersion"] == "2.0" and s["fullAvailable"] is True
    assert [l["nr"] for l in s["lines"]] == [2, 3, 4, 5, 6]
    s = hub.get("/api/services/svc/source?file=src/main/java/probe/Main.java&kind=unit&full=1", headers=H).json()
    assert s["full"] is True and s["totalLines"] == 6 and [l["nr"] for l in s["lines"]] == [1, 2, 3, 4, 5, 6]
    # 当前版本 3.0 还没传源码：详情页据此提示
    assert hub.get("/api/services/svc/detail", headers=H).json()["runtime"]["sourcesUploaded"] is False

    # 源码目录不经静态路径外发，换着写法也绕不过去
    for path in ("/svc/sources/", "/svc/sources/2.0/src/main/java/probe/Main.java",
                 "/svc//sources/2.0/", "/svc/current/../sources/2.0/"):
        assert hub.get(path, headers=H).status_code == 404, path

    # 坏输入：版本串、空正文、没有源码文件、不安全路径
    assert hub.post("/api/upload-sources?service=svc&version=../x", headers=H, content=blob).status_code == 400
    assert hub.post("/api/upload-sources?service=svc&version=2.0", headers=H).status_code == 400
    r = hub.post("/api/upload-sources?service=svc&version=2.0", headers=H, content=_src_tar({"README.md": "x"}))
    assert r.status_code == 400 and "一个源码文件" in r.json()["error"]
    r = hub.post("/api/upload-sources?service=svc&version=2.0", headers=H, content=_src_tar({"../Evil.java": "x"}))
    assert r.status_code == 400
    assert hub.post("/api/upload-sources?service=nope&version=2.0", headers=H, content=blob).status_code == 404


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


def test_detail_with_online_push_instance_is_serializable(hub, monkeypatch):
    """push 服务有在线实例时详情接口要能出 JSON。收集端的连接记录握着 socket，
    曾被原样塞进返回体，详情页一打开就 500（2.6.0 部署后实测）。"""
    import socket
    from covhub import collector as col

    hub.post("/api/services", headers=H, json={"name": "pushed", "channel": "push"})
    pc = col.PushCollector(lambda: {})
    sock = socket.socket()
    pc.conns[7] = {"id": 7, "peer": "10.0.0.8:40001", "sessionid": "pushed", "service": "pushed",
                   "since": "2026-10-08T10:00:00", "last": None, "sock": sock, "rfile": None,
                   "wfile": None, "sessionStart": "2026-10-08T09:59:00", "classIds": {1, 2}}
    monkeypatch.setattr(col, "_COLLECTOR", pc)
    try:
        r = hub.get("/api/services/pushed/detail", headers=H)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["online"] is True and d["instances"] == 1
        assert d["runtime"]["instances"] == [{"id": 7, "peer": "10.0.0.8:40001", "sessionid": "pushed",
                                              "since": "2026-10-08T10:00:00", "last": None,
                                              "sessionStart": "2026-10-08T09:59:00"}]
        r = hub.get("/api/status?service=pushed", headers=H)
        assert r.status_code == 200
        assert r.json()["services"][0]["instances"] == [{"peer": "10.0.0.8:40001",
                                                         "since": "2026-10-08T10:00:00", "last": None}]
        assert hub.get("/api/overview", headers=H).status_code == 200
    finally:
        sock.close()


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


def test_compare_without_snapshots_is_200(hub):
    """刚登记的服务打开「历史对比」：两侧都是 current 且没有任何快照，差值全是 null，不能 500。"""
    hub.post("/api/services", headers=H, json=dict(PULL, version="2.0"))
    r = hub.get("/api/services/svc/compare?a=current&b=current", headers=H)
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["a"]["summary"] is None and c["delta"]["incremental"] is None and c["files"] == []


def test_version_string_validated_before_it_becomes_a_directory(hub):
    """版本串要当目录名：登记 / retarget / predeploy 三个入口都在进库和 dump --reset 之前拦住。"""
    r = hub.post("/api/services", headers=H, json=dict(PULL, version="release/1.4"))
    assert r.status_code == 400 and "目录名" in r.json()["error"]
    r = hub.post("/api/services", headers=H, json=dict(PULL, version="v" * 101))
    assert r.status_code == 400 and "太长" in r.json()["error"]
    hub.post("/api/services", headers=H, json=dict(PULL, version="1.4"))
    r = hub.post("/api/retarget?service=svc&version=release/1.5", headers=H)
    assert r.status_code == 409 and "目录名" in r.text
    assert hub.get("/api/services/svc", headers=H).json()["service"]["version"] == "1.4"
    r = hub.post("/api/predeploy?service=svc&version=release/1.5", headers=H)
    assert r.status_code == 409 and "目录名" in r.text


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


# ---- hub 比对两版源码生成 diff ----

V10 = {"src/main/java/probe/Main.java": "package probe;\n\nclass Main {\n    static void tick() { }\n}\n",
       "src/main/java/probe/Old.java": "package probe;\n\nclass Old {\n    int a() { return 1; }\n    int b() { return 2; }\n"
                                       "    int c() { return 3; }\n    int d() { return 4; }\n}\n"}
V11 = {"src/main/java/probe/Main.java": "package probe;\n\nclass Main {\n    static void tick() { }\n    static void tock() { }\n}\n",
       # Old → Moved：挪代码不算新代码，只有改掉的那一行算
       "src/main/java/probe/Moved.java": "package probe;\n\nclass Moved {\n    int a() { return 1; }\n    int b() { return 2; }\n"
                                         "    int c() { return 3; }\n    int d() { return 4; }\n}\n",
       "src/main/java/probe/Fresh.java": "package probe;\n\nclass Fresh {\n}\n",
       "src/test/java/probe/MainTest.java": "package probe;\nclass MainTest {}\n"}


@pytest.mark.skipif(not shutil.which("git"), reason="hub 侧比对源码要 git")
def test_upload_sources_generates_diff_on_hub(hub, tmp_path):
    """源码按版本传上来之后，diff 由 hub 比对两棵源码树生成：构建节点不需要基线 commit 的历史。"""
    hub.post("/api/services", headers=H, json=dict(PULL, version="1.0"))
    # 第一次接入：还没有基线，源码照收，diff 为空并说明原因
    r = hub.post("/api/upload-sources?service=svc&version=1.0", headers=H, content=_src_tar(V10))
    assert r.status_code == 200, r.text
    assert r.json()["diff"] is None and "基线" in r.json()["diffReason"]

    # 第二版：基线 = 服务当前 version（1.0）
    r = hub.post("/api/upload-sources?service=svc&version=1.1", headers=H, content=_src_tar(V11))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["diffReason"] is None
    d = body["diff"]
    assert d["origin"] == "sources" and d["base"] == "1.0" and d["head"] == "1.1" and d["version"] == "1.1"
    # Main 加一行、Moved 改一行（类名）、Fresh 整个 4 行；.roots.json 与测试代码不算
    assert d["files"] == 3 and d["addedLines"] == 6
    lines = json.loads((tmp_path / "data" / "svc" / "diff" / "1.1.lines.json").read_text(encoding="utf-8"))
    assert lines == {"src/main/java/probe/Main.java": [5], "src/main/java/probe/Moved.java": [3],
                     "src/main/java/probe/Fresh.java": [1, 2, 3, 4]}
    raw = (tmp_path / "data" / "svc" / "diff" / "1.1.diff").read_text(encoding="utf-8")
    # 落盘的 diff 路径是仓库相对的（版本目录名已剥掉），和流水线传的同形
    assert "--- a/src/main/java/probe/Main.java\n+++ b/src/main/java/probe/Main.java\n" in raw
    assert "rename from src/main/java/probe/Old.java\nrename to src/main/java/probe/Moved.java\n" in raw
    assert "1.0/" not in raw and "1.1/" not in raw and ".roots.json" not in raw

    # 显式重做：指定基线
    r = hub.post("/api/diff?service=svc&version=1.1&from=sources&base=1.0", headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["baseReason"] == "调用方指定" and r.json()["diff"]["addedLines"] == 6
    # 不给基线就自动定
    r = hub.post("/api/diff?service=svc&version=1.1&from=sources", headers=H)
    assert r.status_code == 200 and r.json()["baseReason"] == "服务当前 version"
    # 基线没传过源码
    r = hub.post("/api/diff?service=svc&version=1.1&from=sources&base=0.9", headers=H)
    assert r.status_code == 400 and "0.9" in r.json()["error"]

    # 流水线上传的 diff 优先：之后再传源码不会被自动生成的覆盖，但显式 from=sources 可以
    hub.post("/api/diff?service=svc&version=1.1&base=abc123", headers=H, content=DIFF.encode())
    r = hub.post("/api/upload-sources?service=svc&version=1.1", headers=H, content=_src_tar(V11))
    assert r.json()["diff"]["origin"] == "upload" and "不覆盖" in r.json()["diffReason"]
    r = hub.post("/api/diff?service=svc&version=1.1&from=sources", headers=H)
    assert r.json()["diff"]["origin"] == "sources"

    # 调用方要求不生成
    r = hub.post("/api/upload-sources?service=svc&version=1.2&diff=skip", headers=H, content=_src_tar(V11))
    assert r.status_code == 200 and r.json()["diff"] is None and "不生成" in r.json()["diffReason"]

    # 坏参数：from / diff 的取值、上传 git diff 不给 base
    assert hub.post("/api/diff?service=svc&version=1.1&from=git", headers=H).status_code == 400
    assert hub.post("/api/upload-sources?service=svc&version=1.1&diff=maybe", headers=H, content=_src_tar(V11)).status_code == 400
    r = hub.post("/api/diff?service=svc&version=1.1", headers=H, content=DIFF.encode())
    assert r.status_code == 400 and "base" in r.json()["error"]


@pytest.mark.skipif(not shutil.which("git"), reason="hub 侧比对源码要 git")
def test_auto_diff_base_falls_back_to_last_uploaded_sources(hub):
    """服务当前 version 没传过源码时，退到最近上传过源码的版本做基线。"""
    hub.post("/api/services", headers=H, json=dict(PULL, version="9.9"))
    hub.post("/api/upload-sources?service=svc&version=1.0", headers=H, content=_src_tar(V10))
    r = hub.post("/api/upload-sources?service=svc&version=1.1", headers=H, content=_src_tar(V11))
    assert r.status_code == 200 and r.json()["diff"]["base"] == "1.0"
    r = hub.post("/api/diff?service=svc&version=1.1&from=sources", headers=H)
    assert r.json()["baseReason"] == "最近上传过源码的版本"
