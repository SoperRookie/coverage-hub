"""构建期送进来的两样东西：单测 jacoco.xml 与 git diff，以及由它们派生的新增代码覆盖率。

磁盘布局（相对 <dataDir>/<svc>/）：
    unit/<version>/jacoco.xml          单测 XML 原文
    unit/<version>/incremental.json    单测的新增代码覆盖明细
    diff/<version>.diff                diff 原文
    diff/<version>.lines.json          {路径: [新增行号]}，行号明细只落磁盘不进库
    current/incremental.json           运行时的新增代码覆盖明细，随归档 copytree 进 versions/

三个触发点（快照出报告后、单测 XML 到达、diff 到达）都走 recompute()，
调用方负责持 LOCK —— make_report() 先 rmtree 再生成，读到半截报告会算错。
"""

import os
import shutil

from . import incremental as inc
from .db import repo
from .errors import CovhubError
from .layout import ensure_dirs, safe_segment, svc_dir
from .logbuf import log


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


def store_diff(cfg, svc, version, base, head, text):
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
    row = repo.upsert_diff(svc["name"], version, base, head, len(lines), added)
    current = svc.get("version")
    log("%s：收到版本 %s 的 diff（基线 %s）：%d 个源码文件、%d 行新增；服务当前 version=%s，%s"
        % (svc["name"], version, base, len(lines), added, current,
           "匹配" if current == version else "不匹配 —— 运行时快照按 version 找 diff，对不上就算不出新增覆盖"))
    return row, lines


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
    # 只在 sourcefiles 指向的就是这一版时才存片段：给旧版本重算时源码目录已经是新版的了，
    # 存下去的会是错位的代码 —— 宁可没有，也不能把错的当成事实留在归档里
    if svc.get("version") == version:
        attach_snippets(cfg, svc, result)
    inc.write_json(target, result)
    return result


SNIPPET_CONTEXT = 3


def attach_snippets(cfg, svc, result):
    """把每个文件新增行前后几行的源码文本一起存进结果里。

    归档之后源码目录会跟着新版本走，历史版本的「新增代码看源码」不能依赖当时的
    sourcefiles 还在 —— 算增量的那一刻把片段留下来最省事，体积也只有新增行附近几行。
    """
    roots = list(svc.get("sourcefiles") or [])
    base = cfg.get("baseDir")
    for path, entry in result.get("files", {}).items():
        added = entry.get("added") or []
        if not added:
            continue
        candidates = [os.path.join(r, entry.get("reportFile") or path) for r in roots]
        if base:
            candidates.append(os.path.join(base, path))
        text = None
        for cand in candidates:
            if os.path.isfile(cand):
                with open(cand, encoding=svc.get("sourceEncoding", "UTF-8"), errors="replace") as f:
                    text = f.read().splitlines()
                break
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
