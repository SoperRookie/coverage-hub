"""按版本上传的源码：只收源码、按 package 识别源码根、按版本取、配置里的 sourcefiles 只信当前版本。"""
import io
import json
import os
import shutil
import subprocess
import tarfile
import zipfile

import pytest

from covhub import jacoco, sources
from covhub.errors import CovhubError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MAIN = "package com.acme.order;\n\npublic class Main {\n    public static int f(int x) {\n        return x + 1;\n    }\n}\n"


def _tar(path, files, top=None):
    with tarfile.open(path, "w:gz") as tf:
        for name, text in files.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(("%s/%s" % (top, name)) if top else name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return str(path)


def _zip(path, files):
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return str(path)


@pytest.fixture
def env(tmp_path):
    cfg = {"dataDir": str(tmp_path / "data"), "jacocoCli": os.path.join(ROOT, "lib", "jacococli.jar")}
    svc = {"name": "svc", "version": "1.0", "classfiles": []}
    return cfg, svc


def test_store_keeps_only_main_sources(env, tmp_path):
    cfg, svc = env
    blob = _tar(tmp_path / "s.tar.gz", {
        "order-core/src/main/java/com/acme/order/Main.java": MAIN,
        "order-core/src/main/resources/application.yml": "password: hunter2\n",
        "order-core/src/test/java/com/acme/order/MainTest.java": "package com.acme.order;\n",
        "order-api/src/main/kotlin/com/acme/api/Api.kt": "package com.acme.api\n\nclass Api\n",
        "pom.xml": "<project/>",
    })
    out = sources.store_sources(cfg, svc, "1.0", blob)
    assert out["files"] == 2 and out["roots"] == ["order-api/src/main/kotlin", "order-core/src/main/java"]
    base = tmp_path / "data" / "svc" / "sources" / "1.0"
    kept = sorted(os.path.relpath(os.path.join(d, f), base).replace("\\", "/")
                  for d, _, fs in os.walk(base) for f in fs)
    assert kept == [".roots.json", "order-api/src/main/kotlin/com/acme/api/Api.kt",
                    "order-core/src/main/java/com/acme/order/Main.java"]
    assert json.loads((base / ".roots.json").read_text(encoding="utf-8"))["files"] == 2


def test_roots_follow_package_declarations(tmp_path):
    files = {
        "a/src/main/java/com/x/A.java": "package com.x;\nclass A {}\n",
        "weird/java-src/org/y/B.java": "/* 头注释 */\n\npackage org.y;\nclass B {}\n",
        "com/z/C.java": "package com.z;\nclass C {}\n",                 # sources.jar 平铺
        "scripts/D.java": "class D {}\n",                                 # 默认包
        "k/src/main/kotlin/Misplaced.kt": "package com.k\nclass M\n",     # 包与目录对不上，jacococli 也找不到
    }
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    assert sources.detect_roots(str(tmp_path)) == ["", "a/src/main/java", "scripts", "weird/java-src"]


def test_single_module_keeps_its_directory(env, tmp_path):
    """只有一个模块时不能把模块目录当「打包顶层目录」剥掉 —— 它是 diff 路径的一部分。"""
    cfg, svc = env
    sources.store_sources(cfg, svc, "1.0", _tar(tmp_path / "s.tar.gz", {"core/src/main/java/com/acme/order/Main.java": MAIN}))
    text, where = sources.find_source(cfg, svc, "1.0", "core/src/main/java/com/acme/order/Main.java")
    assert where == "sources/1.0" and text[2] == "public class Main {"


def test_bad_upload_keeps_previous_version(env, tmp_path):
    cfg, svc = env
    sources.store_sources(cfg, svc, "1.0", _zip(tmp_path / "ok.zip", {"src/main/java/com/acme/order/Main.java": MAIN}))
    with pytest.raises(RuntimeError):
        sources.store_sources(cfg, svc, "1.0", _zip(tmp_path / "evil.zip", {"../../evil.java": "x"}))
    with pytest.raises(CovhubError):
        sources.store_sources(cfg, svc, "1.0", _zip(tmp_path / "none.zip", {"README.md": "x"}))
    with pytest.raises(CovhubError):
        sources.store_sources(cfg, svc, "../x", _zip(tmp_path / "ok2.zip", {"A.java": "class A {}"}))
    text, where = sources.find_source(cfg, svc, "1.0", "src/main/java/com/acme/order/Main.java")
    assert text[0] == "package com.acme.order;" and where == "sources/1.0"
    parent = tmp_path / "data" / "svc" / "sources"
    assert sorted(os.listdir(parent)) == ["1.0"], "临时目录要清掉"


def test_store_from_directory(env, tmp_path):
    cfg, svc = env
    repo = tmp_path / "repo"
    (repo / "src" / "main" / "java" / "com" / "acme" / "order").mkdir(parents=True)
    (repo / "src" / "main" / "java" / "com" / "acme" / "order" / "Main.java").write_text(MAIN, encoding="utf-8")
    (repo / ".git").mkdir()
    (repo / ".git" / "Stale.java").write_text("x", encoding="utf-8")
    out = sources.store_sources(cfg, svc, "1.0", str(repo))
    assert out["files"] == 1 and out["roots"] == ["src/main/java"]


def test_lookup_order_and_version_trust(env, tmp_path):
    cfg, svc = env
    local = tmp_path / "checkout"
    (local / "com" / "acme" / "order").mkdir(parents=True)
    (local / "com" / "acme" / "order" / "Main.java").write_text("// 本机 checkout 的\n" + MAIN, encoding="utf-8")
    svc = dict(svc, sourcefiles=[str(local)])

    # 没上传：当前版本回落配置的 sourcefiles，旧版本不信它（那个目录已经是新版的了）
    assert sources.source_roots(cfg, svc, "1.0") == [str(local)]
    assert sources.source_roots(cfg, svc, "0.9") == []
    assert sources.find_source(cfg, svc, "0.9", "x/Main.java", "com/acme/order/Main.java") == (None, None)
    text, _ = sources.find_source(cfg, svc, "1.0", "x/Main.java", "com/acme/order/Main.java")
    assert text[0] == "// 本机 checkout 的"

    # 上传了旧版本：旧版本按它来，路径按 diff 的仓库相对路径直接命中，也能按 包/文件 找
    sources.store_sources(cfg, svc, "0.9", _zip(tmp_path / "s.zip", {"core/src/main/java/com/acme/order/Main.java": MAIN}))
    assert sources.source_roots(cfg, svc, "0.9") == [str(tmp_path / "data" / "svc" / "sources" / "0.9" / "core" / "src" / "main" / "java")]
    text, where = sources.find_source(cfg, svc, "0.9", "core/src/main/java/com/acme/order/Main.java")
    assert where == "sources/0.9" and text[0] == "package com.acme.order;"
    text, _ = sources.find_source(cfg, svc, "0.9", "other/Main.java", "com/acme/order/Main.java")
    assert text[0] == "package com.acme.order;"
    # 越界的路径不读
    assert sources.find_source(cfg, svc, "0.9", "../../../../etc/passwd") == (None, None)


def test_make_report_uses_uploaded_roots(env, tmp_path, monkeypatch):
    cfg, svc = env
    calls = []
    monkeypatch.setattr(jacoco, "run_cli", lambda cfg, args, quiet=True: calls.append(list(args)) or "")
    monkeypatch.setattr(jacoco, "summarize", lambda path: {})
    sources.store_sources(cfg, svc, "1.0", _zip(tmp_path / "s.zip", {"m/src/main/java/com/acme/order/Main.java": MAIN}))
    jacoco.make_report(cfg, dict(svc, sourcefiles=["/stale"]), [], str(tmp_path / "out"), "svc")
    args = calls[0]
    roots = [args[i + 1] for i, a in enumerate(args) if a == "--sourcefiles"]
    assert roots == [str(tmp_path / "data" / "svc" / "sources" / "1.0" / "m" / "src" / "main" / "java")]


@pytest.mark.skipif(not shutil.which("javac") or not shutil.which("java"), reason="需要 JDK")
def test_real_report_embeds_uploaded_source(env, tmp_path):
    """真 jacococli：上传的源码进得了 HTML 报告的类页面（下钻到行靠的就是它）。"""
    cfg, svc = env
    src = tmp_path / "repo" / "src" / "main" / "java" / "com" / "acme" / "order"
    src.mkdir(parents=True)
    (src / "Main.java").write_text(MAIN, encoding="utf-8")
    classes = tmp_path / "classes"
    subprocess.run(["javac", "-g", "-d", str(classes), str(src / "Main.java")], check=True)
    sources.store_sources(cfg, svc, "1.0", str(tmp_path / "repo"))
    jacoco.make_report(cfg, dict(svc, classfiles=[str(classes)]), [], str(tmp_path / "out"), "svc")
    page = tmp_path / "out" / "html" / "com.acme.order" / "Main.java.html"
    assert page.is_file() and "return x + 1;" in page.read_text(encoding="utf-8")
