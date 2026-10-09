"""构建期送进来的两样东西：单测 jacoco.xml 与 git diff，以及由它们派生的新增代码覆盖率。

磁盘布局（相对 <dataDir>/<svc>/）：
    unit/<version>/jacoco.xml          单测 XML 原文
    unit/<version>/incremental.json    单测的新增代码覆盖明细
    diff/<version>.diff                diff 原文
    diff/<version>.lines.json          {路径: [新增行号]}，行号明细只落磁盘不进库
    current/incremental.json           运行时的新增代码覆盖明细，随归档 copytree 进 versions/

三个触发点真正共用的是 _incremental()：快照出报告后走 incremental_for_report()、单测 XML
到达走 store_unit_report()，各算自己那一份；只有 diff 到达（和 POST /api/recompute）走
recompute()，把该版本已有的运行时快照和单测报告一起重算。调用方负责持 LOCK ——
make_report() 先 rmtree 再生成，读到半截报告会算错。
"""

import os
import re
import shutil
import subprocess

from . import incremental as inc
from . import sources
from .db import repo
from .errors import CovhubError
from .layout import ensure_dirs, safe_segment, svc_dir
from .logbuf import log
from .sources import find_source

# diffs.origin 的两个取值
DIFF_ORIGIN_UPLOAD = "upload"      # 流水线算好 git diff 传上来
DIFF_ORIGIN_SOURCES = "sources"    # hub 比对两版已上传的源码自己生成


# ---- diff ----

def _diff_paths(cfg, svc, version):
    root = ensure_dirs(cfg, svc)
    v = safe_segment(version)
    return (os.path.join(root, "diff", v + ".diff"),
            os.path.join(root, "diff", v + ".lines.json"))


def load_diff_lines(cfg, svc, version):
    if not version:
        return None
    try:
        _, lines_path = _diff_paths(cfg, svc, version)
    except CovhubError:
        return None            # auto_version 之类的版本串拼不成目录名，那就是没有 diff
    return inc.read_json(lines_path)


def store_diff(cfg, svc, version, base, head, text, origin=DIFF_ORIGIN_UPLOAD):
    """落盘 + 入库，返回 (diff 行, 新增行明细)。"""
    version = safe_segment(version)
    try:
        lines = inc.parse_unified_diff(text)
    except ValueError as exc:
        raise CovhubError(str(exc))
    raw_path, lines_path = _diff_paths(cfg, svc, version)
    with open(raw_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    inc.write_json(lines_path, lines)
    added = sum(len(v) for v in lines.values())
    row = repo.upsert_diff(svc["name"], version, base, head, len(lines), added, origin=origin)
    current = svc.get("version")
    log("%s：收到版本 %s 的 diff（基线 %s）：%d 个源码文件、%d 行新增；服务当前 version=%s，%s"
        % (svc["name"], version, base, len(lines), added, current,
           "匹配" if current == version else "不匹配 —— 运行时快照按 version 找 diff，对不上就算不出新增覆盖"))
    return row, lines


# ---- 由 hub 比对两版源码生成 diff ----
#
# 流水线在构建节点上 git diff，前提是那里有基线 commit 的历史：Jenkins 清过工作区、
# 浅克隆、只拉一个 tag，rev-parse 就找不到基线，diff 静默变空。hub 手里按版本存着
# 源码，两棵树一比就是 diff，和历史深度、工作区死活都没关系。
# 用 git diff --no-index 而不是 difflib：要 -M 的重命名识别（挪代码不算新代码），
# 输出也和流水线那条命令同形，下游解析一字不改。

def _same_version(a, b):
    try:
        return safe_segment(a) == safe_segment(b)
    except CovhubError:
        return a == b


def pick_diff_base(cfg, svc, version):
    """自动定基线：返回 (基线版本, 取自哪里)；定不出时基线为 None、第二项是原因。

    顺序：服务当前运行的版本 → 最近结算的版本 → 最近上传过源码的版本。第一条是
    「本版本相对线上新增了什么」的本意，后两条是它没传过源码时的退让。
    """
    candidates = []
    if svc.get("version"):
        candidates.append((svc["version"], "服务当前 version"))
    for v in reversed(repo.versions(svc["name"], 5)):
        candidates.append((v["version"], "最近结算的版本"))
    for v in sources.uploaded_versions(cfg, svc):
        candidates.append((v["version"], "最近上传过源码的版本"))
    for base, why in candidates:
        if _same_version(base, version):
            continue
        if sources.sources_dir(cfg, svc, base):
            return base, why
    return None, ("找不到上传过源码的基线版本（服务当前 version、最近结算的版本、"
                  "其它上传过源码的版本都没有）—— 第一次接入时属正常，下一版就有了")


def diff_from_sources(cfg, svc, version, base):
    """比对 sources/<base>/ 与 sources/<version>/，返回与流水线同形的 unified diff 文本。"""
    git = shutil.which("git")
    if not git:
        raise CovhubError("hub 上没有 git，没法比对源码生成 diff —— 装 git，或改由流水线上传 git diff")
    head_dir = sources.sources_dir(cfg, svc, version)
    if not head_dir:
        raise CovhubError("版本 %s 没有上传过源码" % version)
    base_dir = sources.sources_dir(cfg, svc, base)
    if not base_dir:
        raise CovhubError("基线版本 %s 没有上传过源码" % base)
    b, h = os.path.basename(base_dir), os.path.basename(head_dir)
    # autocrlf/safecrlf 关掉：这里不是工作区，别让全局配置往 stderr 刷换行告警
    cmd = [git, "-c", "core.quotepath=false", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false",
           "diff", "--no-color", "--no-ext-diff", "--no-textconv", "-M", "--unified=0",
           "--diff-filter=AMR", "--no-index", "--", b, h]
    proc = subprocess.run(cmd, cwd=os.path.dirname(head_dir), capture_output=True)
    if proc.returncode not in (0, 1):      # --no-index：0 无差异、1 有差异，其它才是出错
        raise CovhubError("git diff 失败（退出码 %d）：%s"
                          % (proc.returncode, proc.stderr.decode("utf-8", "replace").strip()))
    return _strip_version_dirs(proc.stdout.decode("utf-8", "replace"), b, h)


def _strip_version_dirs(text, base_dir, head_dir):
    """把 a/<base>/x、b/<head>/x 改回仓库相对路径 a/x、b/x，并丢掉非源码文件（.roots.json）。

    只改文件头那几行，正文一个字节不碰；每个文件段以 diff --git 开头。
    """
    a_pre, b_pre = "a/%s/" % base_dir, "b/%s/" % head_dir
    out = []
    for sec in re.split(r"(?m)^(?=diff --git )", text):
        if not sec:
            continue
        lines = sec.split("\n")
        keep = True
        for i, line in enumerate(lines):
            if line.startswith("@@"):
                break
            if line.startswith("diff --git "):
                # 新增文件两边都是 head 目录（a/<head>/x b/<head>/x），四种前缀都剥
                for pre in (a_pre, "a/%s/" % head_dir, b_pre, "b/%s/" % base_dir):
                    line = line.replace(pre, pre[:2], 1)
                lines[i] = line
            elif line.startswith("--- " + a_pre):
                lines[i] = "--- a/" + line[len("--- " + a_pre):]
            elif line.startswith("+++ " + b_pre):
                lines[i] = "+++ b/" + line[len("+++ " + b_pre):]
                keep = sources.is_main_source(lines[i][6:].rstrip("\t"))
            elif line.startswith("rename from %s/" % base_dir):
                lines[i] = "rename from " + line[len("rename from %s/" % base_dir):]
            elif line.startswith("rename to %s/" % head_dir):
                lines[i] = "rename to " + line[len("rename to %s/" % head_dir):]
        if keep:
            out.append("\n".join(lines))
    return "".join(out)


# ---- 单测报告 ----

def store_unit_report(cfg, svc, version, xml_src, group=None):
    """把上传的 jacoco.xml 存到 unit/<version>/，解析计数器入库；有 diff 就顺手算增量。"""
    version = safe_segment(version)
    root = ensure_dirs(cfg, svc)
    dest_dir = os.path.join(root, "unit", version)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "jacoco.xml")
    try:
        parsed = inc.parse_jacoco(xml_src, group=group)
    except ValueError as exc:
        raise CovhubError("不是能读的 JaCoCo XML：%s" % exc)
    except Exception as exc:
        raise CovhubError("XML 解析失败：%s" % exc)
    shutil.copyfile(xml_src, dest)
    summary = inc.summarize_counters(parsed["counters"])
    result = _incremental(cfg, svc, version, parsed["files"], dest_dir)
    row = repo.upsert_unit_report(svc["name"], version, summary,
                                  "unit/%s/jacoco.xml" % version, result)
    log("%s：收到版本 %s 的单测报告：指令 %.1f%%  分支 %.1f%%  行 %.1f%%%s"
        % (svc["name"], version, summary["instruction"], summary["branch"], summary["line"],
           _inc_text(result)))
    return row, summary, result


# ---- 增量 ----

def _incremental(cfg, svc, version, jacoco_files, out_dir):
    """有该版本的 diff 才算；结果写 out_dir/incremental.json，返回聚合结果（没有 diff 返回 None）。"""
    lines = load_diff_lines(cfg, svc, version)
    target = os.path.join(out_dir, "incremental.json")
    if lines is None:
        if os.path.exists(target):
            os.unlink(target)
        return None
    result = inc.compute(lines, jacoco_files)
    result["version"] = version
    # 片段按版本取源码：上传过这一版的源码就用它；没上传时只有配置里的 sourcefiles，
    # 它只对当前版本可信（给旧版本重算时那个目录已经是新版的了，存下去的会是错位的代码
    # —— 宁可没有，也不能把错的当成事实留在归档里），find_source 会把关
    attach_snippets(cfg, svc, result, version)
    inc.write_json(target, result)
    return result


SNIPPET_CONTEXT = 3


def attach_snippets(cfg, svc, result, version):
    """把每个文件新增行前后几行的源码文本一起存进结果里。

    源码目录可能被清掉、配置里的 sourcefiles 会跟着新版本走 —— 算增量的那一刻把片段
    留下来，历史版本的「新增代码看源码」就总有东西可看，体积也只有新增行附近几行。
    """
    for path, entry in result.get("files", {}).items():
        added = entry.get("added") or []
        if not added:
            continue
        text, _ = find_source(cfg, svc, version, path, entry.get("reportFile"))
        if text is None:
            continue
        wanted = set()
        for nr in added:
            for k in range(nr - SNIPPET_CONTEXT, nr + SNIPPET_CONTEXT + 1):
                if 1 <= k <= len(text):
                    wanted.add(k)
        entry["snippets"] = {str(nr): text[nr - 1] for nr in sorted(wanted)}


def incremental_for_report(cfg, svc, version, report_dir):
    """快照出完报告后调：按 report_dir/jacoco.xml 算这一版新增行的覆盖。"""
    xml = os.path.join(report_dir, "jacoco.xml")
    if not version or not os.path.isfile(xml) or load_diff_lines(cfg, svc, version) is None:
        target = os.path.join(report_dir, "incremental.json")
        if os.path.exists(target):
            os.unlink(target)
        return None
    try:
        parsed = inc.parse_jacoco(xml)
    except Exception as exc:
        log("  ! 读 %s 失败，本次不算新增覆盖：%s" % (xml, exc))
        return None
    result = _incremental(cfg, svc, version, parsed["files"], report_dir)
    if result:
        log("  " + _inc_text(result).lstrip("，"))
    return result


def _inc_text(result):
    if not result:
        return ""
    if result["total"] == 0:
        return "，新增代码：本版本没有可覆盖的新增行"
    return "，新增代码 %.1f%%（%d/%d 行）" % (result["pct"], result["covered"], result["total"])


def recompute(cfg, svc, version):
    """diff 到达（或重传）后，把该版本已有的运行时快照和单测报告重算一遍。

    运行时快照可能已经归档：用 archives.archive_dir 定位 versions/<dir>/jacoco.xml，
    只回写那一条快照的三列，磁盘归档里的 manifest 不动（incremental.json 会更新）。
    """
    root = svc_dir(cfg, svc)
    out = {"runtime": None, "unit": None}

    snap, archive_dir = repo.latest_snapshot_of_version(svc["name"], version)
    if snap is not None:
        report_dir = os.path.join(root, archive_dir) if archive_dir else os.path.join(root, "current")
        result = incremental_for_report(cfg, svc, version, report_dir)
        if result is not None:
            repo.update_snapshot_incremental(snap["id"], result["covered"], result["total"], result["pct"])
            out["runtime"] = _brief(result)
    else:
        log("  版本 %s 还没有运行时快照（构建早于部署是常态），等采集到再算" % version)

    unit = repo.unit_report(svc["name"], version)
    if unit is not None:
        unit_dir = os.path.join(root, "unit", safe_segment(version))
        xml = os.path.join(unit_dir, "jacoco.xml")
        if os.path.isfile(xml):
            parsed = inc.parse_jacoco(xml)
            result = _incremental(cfg, svc, version, parsed["files"], unit_dir)
            repo.update_unit_incremental(svc["name"], version, result)
            out["unit"] = _brief(result) if result else None
    return out


def _brief(result):
    return {"covered": result["covered"], "total": result["total"], "pct": result["pct"],
            "unmatched": len(result["unmatched"]), "ambiguous": len(result["ambiguous"])}


def read_incremental(cfg, svc, where):
    """给详情接口用：读某个目录下的 incremental.json（current/、versions/<dir>/、unit/<v>/）。"""
    return inc.read_json(os.path.join(svc_dir(cfg, svc), where, "incremental.json"))
