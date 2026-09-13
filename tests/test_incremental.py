"""新增代码覆盖率：diff 解析、JaCoCo 行级解析、交集与分母口径。"""
import os

import pytest

from covhub import incremental as inc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_XML = os.path.join(ROOT, "data", "covprobe", "current", "jacoco.xml")


def test_parse_diff_covers_the_nasty_cases():
    text = "\n".join([
        "diff --git a/src/main/java/com/x/Foo.java b/src/main/java/com/x/Foo.java",
        "index 1..2 100644",
        "--- a/src/main/java/com/x/Foo.java",
        "+++ b/src/main/java/com/x/Foo.java",
        "@@ -10,0 +11,2 @@ class Foo {",
        "+    int a = 1;",
        "+++i;",                                  # 正文行以 +++ 开头，不是文件头
        "@@ -20,3 +23,2 @@",
        " ctx", "-old", "-old2", "+new", " ctx",   # 带上下文的普通 diff
        "diff --git a/README.md b/README.md",     # 非源码，跳过
        "--- a/README.md", "+++ b/README.md", "@@ -1 +1,2 @@", " x", "+y",
        "diff --git a/src/test/java/T.java b/src/test/java/T.java",
        "--- /dev/null", "+++ b/src/test/java/T.java", "@@ -0,0 +1,2 @@", "+a", "+b",
        "\\ No newline at end of file",
        'diff --git "a/src/main/java/com/x/\\344\\270\\255.java" "b/src/main/java/com/x/\\344\\270\\255.java"',
        '--- "a/src/main/java/com/x/\\344\\270\\255.java"',
        '+++ "b/src/main/java/com/x/\\344\\270\\255.java"',  # core.quotepath 的八进制转义
        "@@ -0,0 +1 @@", "+z",
        "diff --git a/src/main/java/Gone.java b/src/main/java/Gone.java",
        "deleted file mode 100644",
        "--- a/src/main/java/Gone.java", "+++ /dev/null", "@@ -1,2 +0,0 @@", "-a", "-b",
        "diff --git a/src/main/java/Old.java b/src/main/java/New.java",
        "similarity index 100%", "rename from src/main/java/Old.java", "rename to src/main/java/New.java",
        "diff --git a/img.png b/img.png", "Binary files a/img.png and b/img.png differ",
        "",
    ])
    got = inc.parse_unified_diff(text.replace("\n", "\r\n"))       # Windows agent 的 CRLF
    assert got == {
        "src/main/java/com/x/Foo.java": [11, 12, 24],
        "src/test/java/T.java": [1, 2],
        "src/main/java/com/x/中.java": [1],
    }


def test_parse_diff_noprefix_and_edge_cases():
    assert inc.parse_unified_diff("--- x.java\n+++ x.java\n@@ -1 +1 @@\n-a\n+b\n") == {"x.java": [1]}
    assert inc.parse_unified_diff("--- a/x.java\n+++ b/x.java\n@@ -1,2 +1,0 @@\n-a\n-b\n") == {}
    assert inc.parse_unified_diff("--- a/x.java\t\n+++ b/x y.java\t\n@@ -0,0 +1 @@\n+q\n") == {"x y.java": [1]}
    assert inc.parse_unified_diff("") == {}
    with pytest.raises(ValueError):
        inc.parse_unified_diff("not a diff at all")


def test_normalize_path():
    assert inc.normalize_path("order-core/src/main/java/com/x/Foo.java") == ("order-core", "main", "com/x/Foo.java")
    assert inc.normalize_path("src/test/kotlin/com/x/FooTest.kt") == (None, "test", "com/x/FooTest.kt")
    assert inc.normalize_path("lib/Foo.java") == (None, None, None)


def test_compute_denominator_and_matching():
    jacoco = {
        ("", "com/x/Foo.java"): {11: (0, 3), 12: (2, 0), 13: (1, 1)},      # 13 部分覆盖也算覆盖
        ("core", "com/x/Dup.java"): {1: (0, 1)},
        ("api", "com/x/Dup.java"): {1: (1, 0)},
    }
    diff = {
        "src/main/java/com/x/Foo.java": [10, 11, 12, 13, 14],   # 10、14 没探针，不进分母
        "core/src/main/java/com/x/Dup.java": [1],               # 多模块同名：靠 module≈group 消歧
        "src/test/java/com/x/FooTest.java": [5],                # 测试代码整个丢掉
        "src/main/java/com/x/Excluded.java": [7],               # 报告里没有 → unmatched
        "weird/layout/com/x/Foo.java": [11],                    # 规整不了，剥前缀命中，与上面合并
    }
    r = inc.compute(diff, jacoco)
    assert (r["covered"], r["total"]) == (3, 4)
    assert r["pct"] == 75.0
    assert r["unmatched"] == ["src/main/java/com/x/Excluded.java"]
    assert r["skipped"] == ["src/test/java/com/x/FooTest.java"]
    assert [a["file"] for a in r["ambiguous"]] == ["com/x/Foo.java"]
    assert r["files"]["com/x/Foo.java"]["missed"] == [12]
    assert r["files"]["core/src/main/java/com/x/Dup.java"]["group"] == "core"


def test_compute_empty_diff_gives_none_pct():
    r = inc.compute({}, {("", "a/B.java"): {1: (0, 1)}})
    assert r["total"] == 0 and r["pct"] is None


@pytest.mark.skipif(not os.path.isfile(SAMPLE_XML), reason="没有样本 jacoco.xml")
def test_parse_flat_jacoco_xml():
    parsed = inc.parse_jacoco(SAMPLE_XML)
    assert parsed["counters"]["INSTRUCTION"]["missed"] + parsed["counters"]["INSTRUCTION"]["covered"] > 0
    key = next(iter(parsed["files"]))
    assert key[0] == "" and key[1].endswith(".java")
    summary = inc.summarize_counters(parsed["counters"])
    assert set(summary) >= {"instruction", "branch", "line", "covered", "total", "classes_hit"}


def test_parse_grouped_jacoco_xml(tmp_path):
    xml = tmp_path / "agg.xml"
    xml.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<report name="agg">
  <group name="agg">
    <group name="core">
      <package name="com/x">
        <sourcefile name="Foo.java"><line nr="1" mi="0" ci="2" mb="0" cb="0"/><counter type="LINE" missed="0" covered="1"/></sourcefile>
        <counter type="LINE" missed="0" covered="1"/>
      </package>
      <counter type="INSTRUCTION" missed="1" covered="3"/>
      <counter type="LINE" missed="0" covered="1"/>
    </group>
    <group name="api">
      <package name="com/y">
        <sourcefile name="Bar.java"><line nr="5" mi="1" ci="0" mb="0" cb="0"/></sourcefile>
      </package>
      <counter type="INSTRUCTION" missed="4" covered="0"/>
    </group>
  </group>
  <counter type="INSTRUCTION" missed="5" covered="3"/>
  <counter type="LINE" missed="1" covered="1"/>
</report>""", encoding="utf-8")
    parsed = inc.parse_jacoco(str(xml))
    assert set(parsed["files"]) == {("core", "com/x/Foo.java"), ("api", "com/y/Bar.java")}
    assert parsed["counters"]["INSTRUCTION"] == {"missed": 5, "covered": 3}
    only = inc.parse_jacoco(str(xml), group="core")
    assert set(only["files"]) == {("core", "com/x/Foo.java")}
    assert only["counters"]["INSTRUCTION"] == {"missed": 1, "covered": 3}
    with pytest.raises(ValueError):
        inc.parse_jacoco(str(xml), group="nope")
    bad = tmp_path / "bad.xml"
    bad.write_text("<html/>", encoding="utf-8")
    with pytest.raises(ValueError):
        inc.parse_jacoco(str(bad))
