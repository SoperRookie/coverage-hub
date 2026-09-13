"""新增代码的覆盖率：git diff 的新增行 × JaCoCo 报告的行级数据。

分母口径与 SonarQube 的「New Lines to Cover」一致：diff 新增行里**JaCoCo 有探针记录
的行**才算（空行、注释、import、纯声明没有探针，本来就不参与覆盖率）；分子是其中
`ci > 0` 的行（含部分覆盖，和 LINE 计数器同口径）。删除的行、只改不增的行不参与。
`total == 0` 时 pct 是 None —— 空 diff（只改了 yaml）是合法的，前端显示「—」。

diff 里有、报告里找不到的源码文件单独列出（unmatched）：类被 excludes 排掉时，新增行
会静默从分母消失，页面显示"新增覆盖 100%"却一行没测 —— 这比算错更糟。
"""

import json
import os
import re
import xml.etree.ElementTree as ET

SOURCE_EXT = (".java", ".kt", ".groovy", ".scala")
# Maven / Gradle 的标准布局：<module>/src/<sourceSet>/<lang>/<pkg>/File.ext
SRC_ROOT_RE = re.compile(r"^(?:(?P<module>.+?)/)?src/(?P<set>[^/]+)/(?:java|kotlin|scala|groovy)/(?P<rel>.+)$")
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")   # 不锚 $：后面还有函数上下文


# ---- git diff ----

def parse_unified_diff(text):
    """返回 {新文件路径: [新增行号]}。

    只认紧跟 `--- ` / `diff --git` 之后的 `+++ ` 为文件头 —— 正文里新增一行 `++i;`
    在 diff 里就是 `+++i;`，按"行以 +++ 开头"判断会把它当成新文件。行号靠逐行走
    hunk（上下文 +1、新增记录并 +1、删除不动），不信 hunk 头的长度，-U0 / -U3 /
    手拷的 diff 都能吃。
    """
    files = {}
    current = None          # 当前文件的行号列表；None = 不在源码文件的 hunk 里
    new_line = 0
    expect_header = False   # 刚见过 --- 或 diff --git，下一行的 +++ 才是文件头
    seen_hunk = False
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if line.startswith("diff --git ") or line.startswith("--- "):
            expect_header = True
            current = None
            continue
        if expect_header and line.startswith("+++ "):
            expect_header = False
            path = _header_path(line[4:])
            current = files.setdefault(path, []) if path and _is_source(path) else None
            continue
        expect_header = False
        m = HUNK_RE.match(line)
        if m:
            seen_hunk = True
            new_line = int(m.group(1))
            continue
        if current is None and not seen_hunk:
            continue
        if line.startswith("\\ No newline") or line.startswith("Binary files") \
                or line.startswith("Subproject commit") or line.startswith("index ") \
                or line.startswith("old mode") or line.startswith("new mode") \
                or line.startswith("similarity index") or line.startswith("rename ") \
                or line.startswith("new file mode") or line.startswith("deleted file mode"):
            continue
        if line.startswith("+"):
            if current is not None:
                current.append(new_line)
            new_line += 1
        elif line.startswith("-"):
            pass
        elif line.startswith(" ") or line == "":
            new_line += 1
    if text.strip() and not seen_hunk:
        raise ValueError("正文里没有任何 @@ hunk，不像是 git diff 的输出")
    return {path: sorted(set(lines)) for path, lines in files.items() if lines}


def _header_path(rest):
    """`+++ ` 之后的部分：去掉行尾 tab（路径含空格时 git 会追加）、引号与八进制转义、b/ 前缀。"""
    rest = rest.rstrip("\t").strip()
    if rest == "/dev/null":
        return None
    if rest.startswith('"') and rest.endswith('"'):
        rest = _unquote_c(rest[1:-1])
    if rest.startswith("b/"):
        rest = rest[2:]
    return rest


def _unquote_c(s):
    """git 对非 ASCII 路径的 C 风格转义（core.quotepath=true 的默认输出）。"""
    out = bytearray()
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt in "01234567" and i + 3 < len(s):
                out.extend([int(s[i + 1:i + 4], 8)])
                i += 4
                continue
            simple = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}.get(nxt)
            if simple:
                out.extend(simple.encode("utf-8"))
                i += 2
                continue
        out.extend(c.encode("utf-8"))
        i += 1
    return out.decode("utf-8", "replace")


def _is_source(path):
    return path.lower().endswith(SOURCE_EXT)


def normalize_path(path):
    """按标准布局拆成 (module, sourceSet, 包路径/文件)。拆不出来返回 (None, None, None)。"""
    m = SRC_ROOT_RE.match(path)
    if not m:
        return None, None, None
    return m.group("module"), m.group("set"), m.group("rel")


# ---- JaCoCo XML ----

def parse_jacoco(xml_path, group=None):
    """流式读一份 jacoco.xml，返回 {"counters": {...}, "files": {(group, "pkg/File.java"): {nr: (mi, ci)}}}。

    hub 自己出的报告是 report > package；report-aggregate 的是 report > group(title)
    > group(artifactId) > package —— 用一个 group 名字栈，深度无关。`group=` 给了就只收
    那个模块（聚合报告的 <group name=artifactId>），计数器也取那个 group 的。
    iterparse + clear()：几十 MB 的聚合 XML 用 ET.parse 会吃几百 MB 内存。
    """
    files = {}
    groups = []              # 当前 group 名字栈
    package = None
    sourcefile = None
    lines = None
    top_counters = {}
    group_counters = {}      # group 名 -> counters
    stack = []
    root_checked = False
    for event, elem in ET.iterparse(xml_path, events=("start", "end")):
        if event == "start":
            stack.append(elem.tag)
            if not root_checked:
                root_checked = True
                if elem.tag != "report":
                    raise ValueError("根元素是 <%s>，不是 JaCoCo 的 <report>" % elem.tag)
            if elem.tag == "group":
                groups.append(elem.get("name", ""))
            elif elem.tag == "package":
                package = elem.get("name", "")
            elif elem.tag == "sourcefile":
                sourcefile = elem.get("name", "")
                lines = {}
            continue
        # end
        tag = elem.tag
        if tag == "line" and lines is not None:
            lines[int(elem.get("nr"))] = (int(elem.get("mi", 0)), int(elem.get("ci", 0)))
        elif tag == "sourcefile":
            wanted = group is None or (groups and groups[-1] == group)
            if wanted and lines:
                key = (groups[-1] if groups else "", "%s/%s" % (package, sourcefile))
                files[key] = lines
            sourcefile, lines = None, None
            elem.clear()
        elif tag == "class" or tag == "method":
            elem.clear()
        elif tag == "package":
            package = None
            elem.clear()
        elif tag == "counter":
            parent = stack[-2] if len(stack) >= 2 else ""
            entry = {"missed": int(elem.get("missed", 0)), "covered": int(elem.get("covered", 0))}
            if parent == "report":
                top_counters[elem.get("type")] = entry
            elif parent == "group" and groups:
                group_counters.setdefault(groups[-1], {})[elem.get("type")] = entry
        elif tag == "group":
            groups.pop()
            elem.clear()
        stack.pop()
    counters = group_counters.get(group) if group else top_counters
    if group and not counters:
        raise ValueError("报告里没有名为 %r 的 group（模块）" % group)
    return {"counters": counters or {}, "files": files}


def summarize_counters(counters):
    """把 JaCoCo 计数器折成入库用的摘要（和 jacoco.summarize 的口径一致）。"""
    def pct(kind):
        c = counters.get(kind, {"missed": 0, "covered": 0})
        total = c["missed"] + c["covered"]
        return round(c["covered"] * 100.0 / total, 2) if total else 0.0

    def pair(kind):
        c = counters.get(kind, {"missed": 0, "covered": 0})
        return c["covered"], c["missed"] + c["covered"]

    ins_c, ins_t = pair("INSTRUCTION")
    ln_c, ln_t = pair("LINE")
    cls_c, cls_t = pair("CLASS")
    return {"instruction": pct("INSTRUCTION"), "branch": pct("BRANCH"), "line": pct("LINE"),
            "covered": ins_c, "total": ins_t, "lines_covered": ln_c, "lines_total": ln_t,
            "classes_hit": cls_c, "classes_total": cls_t}


# ---- 交集 ----

def compute(diff_lines, jacoco_files):
    """diff 的新增行 × 报告的行级数据。

    路径匹配：先按标准布局规整成 (module, pkg/File)，聚合报告优先用 (group≈module, pkg/File)
    二元键；规整不了的（Gradle 自定义 sourceSet、根目录裸放）退到「逐段剥前缀直到命中」。
    多条 diff 路径落到同一个报告文件时合并行号并标 ambiguous（运行时报告里同包同名的文件
    本来就已被 JaCoCo 合并成一个，XML 层无法再分开）。
    """
    by_rel = {}
    for (grp, rel), lines in jacoco_files.items():
        by_rel.setdefault(rel, []).append((grp, lines))

    per_key = {}           # 报告文件键 -> {"paths": [...], "lines": set()}
    unmatched, skipped = [], []
    for path, added in sorted(diff_lines.items()):
        module, sset, rel = normalize_path(path)
        if sset is not None and sset != "main":
            skipped.append(path)         # 测试代码不进分母
            continue
        hits = _lookup(by_rel, rel, path, module)
        if not hits:
            unmatched.append(path)
            continue
        for key in hits:
            slot = per_key.setdefault(key, {"paths": [], "lines": set()})
            slot["paths"].append(path)
            slot["lines"].update(added)

    covered = total = 0
    files = {}
    ambiguous = []
    for key, slot in per_key.items():
        probe = jacoco_files[key]
        f_cov = f_tot = 0
        missed = []
        for nr in sorted(slot["lines"]):
            hit = probe.get(nr)
            if hit is None:
                continue
            f_tot += 1
            if hit[1] > 0:
                f_cov += 1
            else:
                missed.append(nr)
        covered += f_cov
        total += f_tot
        label = slot["paths"][0] if len(slot["paths"]) == 1 else key[1]
        if len(slot["paths"]) > 1:
            ambiguous.append({"file": key[1], "paths": slot["paths"]})
        files[label] = {"covered": f_cov, "total": f_tot, "missed": missed,
                        "reportFile": key[1], "group": key[0] or None}
    return {
        "covered": covered, "total": total,
        "pct": round(covered * 100.0 / total, 2) if total else None,
        "files": files, "ambiguous": ambiguous, "unmatched": unmatched,
        "skipped": skipped,
    }


def _lookup(by_rel, rel, path, module):
    """返回命中的报告文件键列表。"""
    candidates = None
    if rel and rel in by_rel:
        candidates = by_rel[rel]
        chosen_rel = rel
    else:
        parts = path.split("/")
        for i in range(len(parts)):
            tail = "/".join(parts[i:])
            if tail in by_rel:
                candidates = by_rel[tail]
                chosen_rel = tail
                break
    if not candidates:
        return []
    if len(candidates) > 1 and module:
        exact = [grp for grp, _ in candidates if grp == module or grp.endswith("/" + module)]
        if exact:
            return [(grp, chosen_rel) for grp in exact]
    return [(grp, chosen_rel) for grp, _ in candidates]


# ---- 落盘 ----

def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def read_json(path):
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)
