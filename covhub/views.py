"""给前端看的聚合视图：首页总览与服务详情。

只读、只查库和磁盘上的 incremental.json，**请求路径上不做 TCP 探活**（在线状态由
采集线程每轮写进 service_state）。age / stale 在服务端算：库里的时刻是本地时间的
ISO 串、不带时区，浏览器自己减会差出时区来。
"""

import os
from datetime import datetime

from . import build
from .agent import endpoint_label, service_channel
from .collector import collector_instances, get_collector
from .config import find_service
from .db import repo
from .errors import CovhubError
from .layout import safe_segment, svc_dir


def _age_seconds(iso):
    if not iso:
        return None
    try:
        return int((datetime.now() - datetime.fromisoformat(iso)).total_seconds())
    except (ValueError, TypeError):
        return None


def _brief(entry):
    """快照 / 单测报告里前端要的那几个数：总覆盖 + 新增覆盖。"""
    if not entry:
        return None
    out = {"at": entry.get("at"), "version": entry.get("version"),
           "instruction": entry.get("instruction"), "branch": entry.get("branch"),
           "covered": entry.get("covered"), "total": entry.get("total"),
           "classesHit": entry.get("classesHit"), "classesTotal": entry.get("classesTotal")}
    if "line" in entry:
        out["line"] = entry["line"]
        out["linesCovered"], out["linesTotal"] = entry.get("linesCovered"), entry.get("linesTotal")
    if entry.get("incTotal") is not None:
        out["incremental"] = {"covered": entry["incCovered"], "total": entry["incTotal"],
                              "pct": entry.get("incPct")}
    else:
        out["incremental"] = None
    return out


def service_row(cfg, svc, stale_after):
    """总览里的一行。运维状态（在线 / 采集停了 / 断代 / 混版本）是语义色的唯一来源。"""
    name = svc["name"]
    channel = service_channel(svc)
    latest = repo.latest(name)
    age = _age_seconds(latest["at"]) if latest else None

    if channel == "push":
        # 收集端在本进程时能直接数在线实例；不在（别的进程）就如实说不知道
        if get_collector() is None:
            online, unknown = None, True
        else:
            online, unknown = bool(collector_instances(name)), False
        online_at = None
    else:
        online, online_at = repo.get_online(name)
        unknown = online is None

    state = repo.get_state(name)
    unit = repo.latest_unit_report(name)
    diff = repo.latest_diff(name)
    return {
        "name": name,
        "project": svc.get("project"),
        "channel": channel,
        "endpoint": endpoint_label(svc),
        "version": svc.get("version"),
        "online": online,
        "unknown": unknown,
        "onlineAt": online_at,
        "instances": len(collector_instances(name)) if channel == "push" else None,
        "ageSeconds": age,
        "stale": age is not None and age > stale_after,
        "pushMixed": state["pushMixed"],
        "runtime": _brief(latest),
        "unit": _brief(unit),
        "diff": {"version": diff["version"], "base": diff["base"], "addedLines": diff["addedLines"],
                 "files": diff["files"], "at": diff["at"]} if diff else None,
        "breaks": len(repo.breaks(name, 3)),
        "hasReport": os.path.isfile(os.path.join(svc_dir(cfg, svc), "current", "html", "index.html")),
    }


def overview(cfg):
    interval = int((cfg.get("watch") or {}).get("intervalSeconds", 300))
    stale_after = max(interval * 3, 900)
    rows = {svc["name"]: service_row(cfg, svc, stale_after) for svc in cfg.get("services", [])}

    projects = []
    assigned = set()
    for proj in repo.list_projects():
        members = [rows[n] for n in proj["services"] if n in rows]
        assigned.update(proj["services"])
        projects.append({
            "name": proj["name"], "title": proj.get("title"), "description": proj.get("description"),
            "services": members,
            # 项目行只放计数，不算平均覆盖率 —— 把各服务的百分比平均起来只会误导
            "counts": _counts(members),
        })
    unassigned = [row for name, row in rows.items() if name not in assigned]
    return {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "staleAfterSeconds": stale_after,
        "projects": projects,
        "unassigned": unassigned,
        "counts": _counts(list(rows.values())),
    }


def _counts(rows):
    return {
        "services": len(rows),
        "online": sum(1 for r in rows if r["online"]),
        "offline": sum(1 for r in rows if r["online"] is False),
        "unknown": sum(1 for r in rows if r["unknown"]),
        "stale": sum(1 for r in rows if r["stale"]),
        "attention": sum(1 for r in rows if r["stale"] or r["breaks"] or r["pushMixed"]),
    }


def service_detail(cfg, name, version=None):
    """version 给的是归档目录名（如 1.4.2 或 1.4.2-2）时，运行时那一栏切到那个已结算版本：
    数字来自结算快照，新增代码明细来自归档目录里的 incremental.json，报告链接指向归档。"""
    svc = find_service(cfg, name)
    interval = int((cfg.get("watch") or {}).get("intervalSeconds", 300))
    row = service_row(cfg, svc, max(interval * 3, 900))
    versions = repo.versions(name, 20)
    latest = repo.latest(name)

    base = "/%s" % name
    viewing = None
    if version:
        archived = repo.archive_by_dir(name, "versions/%s" % safe_segment(version))
        if archived is None:
            raise CovhubError("没有归档 %s" % version)
        viewing = archived
        latest = archived
        runtime_inc = build.read_incremental(cfg, svc, "versions/%s" % archived["dir"])
        report_dir = "%s/versions/%s" % (base, archived["dir"])
        has_report = os.path.isfile(os.path.join(svc_dir(cfg, svc), "versions", archived["dir"], "html", "index.html"))
        unit = repo.unit_report(name, archived["version"])
    else:
        runtime_inc = build.read_incremental(cfg, svc, "current")
        report_dir = "%s/current" % base
        has_report = row["hasReport"]
        unit = repo.latest_unit_report(name)
    unit_inc = build.read_incremental(cfg, svc, "unit/%s" % unit["version"]) if unit else None

    return {
        **row,
        "viewingVersion": viewing,
        "config": {k: svc.get(k) for k in ("includes", "excludes", "classfiles", "sourcefiles",
                                          "reportExcludes", "classDumpDir")},
        "runtime": {
            "latest": _brief(latest),
            "history": [_brief(h) | {"kind": h["kind"]} for h in repo.history(name, 40)],
            "versions": [_brief(v) | {"dir": v["dir"], "sealedAt": v["sealedAt"],
                                      "sealedBy": v["sealedBy"], "matchRate": v.get("matchRate"),
                                      "reportUrl": "%s/versions/%s/html/index.html" % (base, v["dir"]),
                                      "xmlUrl": "%s/versions/%s/jacoco.xml" % (base, v["dir"])}
                         for v in versions],
            "breaks": repo.breaks(name, 10),
            # 历史版本选择器用：连重启封存的也列出来（它们不算「已结算版本」，但数据在）
            "archives": [{"version": a["version"], "dir": a["dir"], "sealedAt": a["sealedAt"],
                          "sealedBy": a["sealedBy"], "at": a["at"]}
                         for a in reversed(repo.versions(name, 100, sealed_by=None))],
            "instances": collector_instances(name) if row["channel"] == "push" else [],
            "incremental": _files_view(runtime_inc),
            "reportUrl": "%s/html/index.html" % report_dir if has_report else None,
            "xmlUrl": "%s/jacoco.xml" % report_dir,
        },
        "unit": {
            "latest": _brief(unit),
            "history": [_brief(u) for u in repo.unit_history(name, 40)],
            "incremental": _files_view(unit_inc),
            "xmlUrl": ("%s/%s" % (base, unit["xmlPath"])) if unit else None,
        },
    }


def _files_view(result):
    """incremental.json 的按文件明细，转成表格好用的形态。"""
    if not result:
        return None
    files = []
    for path, f in sorted(result.get("files", {}).items(), key=lambda kv: (kv[1]["covered"] == kv[1]["total"], kv[0])):
        files.append({"path": path, "covered": f["covered"], "total": f["total"],
                      "pct": round(f["covered"] * 100.0 / f["total"], 1) if f["total"] else None,
                      "missed": f["missed"], "group": f.get("group")})
    return {"covered": result["covered"], "total": result["total"], "pct": result.get("pct"),
            "version": result.get("version"), "files": files,
            "unmatched": result.get("unmatched", []), "ambiguous": result.get("ambiguous", []),
            "skipped": len(result.get("skipped", []))}


def versions_for_pipeline(cfg, name):
    """流水线先问「上一版是谁」：最近归档的版本与它的 diff 头。"""
    find_service(cfg, name)
    versions = repo.versions(name, 5)
    out = []
    for v in reversed(versions):
        diff = repo.get_diff(name, v["version"])
        out.append({"version": v["version"], "dir": v["dir"], "sealedAt": v["sealedAt"],
                    "head": diff["head"] if diff else None, "base": diff["base"] if diff else None})
    return {"service": name, "versions": out, "latest": out[0] if out else None}


def _report_page(report_file):
    """JaCoCo HTML 报告里源码页的相对路径：包目录用点号，文件名加 .java.html。"""
    pkg, _, name = report_file.rpartition("/")
    return "%s/%s.html" % (pkg.replace("/", ".") if pkg else "default", name)


def _recompute_entry(cfg, svc, where, version, path):
    from . import incremental as inc
    xml = os.path.join(svc_dir(cfg, svc), where, "jacoco.xml")
    lines = build.load_diff_lines(cfg, svc, version) if version else None
    if lines is None or not os.path.isfile(xml):
        return None
    try:
        fresh = inc.compute(lines, inc.parse_jacoco(xml)["files"])
    except Exception:
        return None
    return fresh.get("files", {}).get(path)


def incremental_source(cfg, name, kind, path, context=3, version=None):
    """某个文件的新增代码源码视图：新增行标覆盖状态，前后带 context 行上下文。

    源码优先用算增量时存进 incremental.json 的片段（历史版本靠它，不依赖当时的源码目录
    还在）；没有片段再从服务的 sourcefiles 里找；都没有就只返回行号与状态。
    """
    svc = find_service(cfg, name)
    where = "current"
    if kind == "unit":
        if version:
            archived = repo.archive_by_dir(name, "versions/%s" % safe_segment(version))
            unit = repo.unit_report(name, archived["version"]) if archived else None
        else:
            unit = repo.latest_unit_report(name)
        if not unit:
            raise CovhubError("还没有单测报告")
        where = "unit/%s" % unit["version"]
    elif version:
        where = "versions/%s" % safe_segment(version)
    result = build.read_incremental(cfg, svc, where)
    if not result or path not in result.get("files", {}):
        raise CovhubError("没有 %s 的新增代码明细" % path)
    entry = result["files"][path]
    if "added" not in entry:
        # 2.1 早期写的 incremental.json 只有 missed 没有 added / hit：用同目录的 jacoco.xml
        # 和还在磁盘上的 diff 现算一遍（只读，不回写 —— 回写走 recompute）
        entry = _recompute_entry(cfg, svc, where, result.get("version"), path) or entry
    report_file = entry.get("reportFile") or path
    added = set(entry.get("added") or [])
    hits, missed = set(entry.get("hit") or []), set(entry.get("missed") or [])

    text_lines, source_path = None, None
    snippets = entry.get("snippets")
    if snippets:
        # 片段是稀疏的 {行号: 文本}；铺成按行号索引的列表，缺的行留 None
        max_nr = max(int(k) for k in snippets)
        text_lines = [None] * max_nr
        for k, v in snippets.items():
            text_lines[int(k) - 1] = v
        source_path = "incremental.json"
    else:
        candidates = [os.path.join(root, report_file) for root in svc.get("sourcefiles", [])]
        candidates.append(os.path.join(cfg.get("baseDir", ""), path))
        for cand in candidates:
            if os.path.isfile(cand):
                with open(cand, encoding=svc.get("sourceEncoding", "UTF-8"), errors="replace") as f:
                    text_lines = f.read().splitlines()
                source_path = cand
                break

    def status(nr):
        if nr not in added:
            return "context"
        if nr in hits:
            return "covered"
        if nr in missed:
            return "missed"
        return "nocode"          # 新增行但 JaCoCo 没探针：空行、注释、声明

    lines = []
    if text_lines is not None:
        wanted = set()
        for nr in added:
            for k in range(nr - context, nr + context + 1):
                if 1 <= k <= len(text_lines):
                    wanted.add(k)
        prev = 0
        for nr in sorted(wanted):
            if text_lines[nr - 1] is None:
                continue                    # 片段里没有这一行（超出当时的上下文范围）
            if prev and nr != prev + 1:
                lines.append({"nr": None, "text": "…", "status": "gap"})
            lines.append({"nr": nr, "text": text_lines[nr - 1], "status": status(nr)})
            prev = nr
    else:
        for nr in sorted(added):
            lines.append({"nr": nr, "text": None, "status": status(nr)})

    base = "/%s" % name
    if kind != "runtime":
        report_dir = None
    elif version:
        report_dir = "%s/versions/%s/html" % (base, safe_segment(version))
    else:
        report_dir = "%s/current/html" % base
    return {
        "service": name, "kind": kind, "path": path, "reportFile": report_file,
        "sourceFound": text_lines is not None, "sourcePath": source_path,
        "covered": entry["covered"], "total": entry["total"], "added": len(added),
        "lines": lines,
        "reportUrl": ("%s/%s" % (report_dir, _report_page(report_file))) if report_dir else None,
    }


def project_report(cfg, project, days=30):
    """项目维度的报表：每个服务的最新数字 + 时间范围内的已结算版本与单测报告。

    days=0 表示不限时间。不算项目平均覆盖率 —— 各服务的百分比平均起来只会误导，
    报表给的是逐服务、逐版本的原始数字，汇总由看的人按自己的口径做。
    """
    from datetime import timedelta
    if project == "__unassigned":
        names = [s["name"] for s in cfg.get("services", []) if not s.get("project")]
        title = "未分组"
    else:
        proj = repo.get_project(project)
        names = proj["services"]
        title = proj.get("title") or project
    since = datetime.now() - timedelta(days=days) if days and days > 0 else None
    interval = int((cfg.get("watch") or {}).get("intervalSeconds", 300))
    stale_after = max(interval * 3, 900)
    by_name = {s["name"]: s for s in cfg.get("services", [])}

    services = []
    for name in names:
        svc = by_name.get(name)
        if not svc:
            continue
        row = service_row(cfg, svc, stale_after)
        versions = repo.versions_since(name, since)
        units = repo.unit_reports_since(name, since)
        services.append({
            "name": name, "channel": row["channel"], "version": row["version"],
            "online": row["online"], "unknown": row["unknown"], "stale": row["stale"],
            "breaks": row["breaks"], "ageSeconds": row["ageSeconds"],
            "runtime": row["runtime"], "unit": row["unit"],
            "versions": [_brief(v) | {"dir": v["dir"], "sealedAt": v["sealedAt"],
                                      "matchRate": v.get("matchRate"),
                                      "reportUrl": "/%s/versions/%s/html/index.html" % (name, v["dir"]),
                                      "xmlUrl": "/%s/versions/%s/jacoco.xml" % (name, v["dir"])}
                         for v in versions],
            "unitReports": [_brief(u) for u in units],
        })
    return {
        "project": project, "title": title, "days": days,
        "since": since.isoformat(timespec="seconds") if since else None,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "services": services,
        "counts": _counts([service_row(cfg, by_name[n], stale_after) for n in names if n in by_name]),
    }
