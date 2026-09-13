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
from .layout import svc_dir


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


def service_detail(cfg, name):
    svc = find_service(cfg, name)
    interval = int((cfg.get("watch") or {}).get("intervalSeconds", 300))
    row = service_row(cfg, svc, max(interval * 3, 900))
    versions = repo.versions(name, 20)
    latest = repo.latest(name)

    runtime_inc = build.read_incremental(cfg, svc, "current")
    unit = repo.latest_unit_report(name)
    unit_inc = build.read_incremental(cfg, svc, "unit/%s" % unit["version"]) if unit else None

    base = "/%s" % name
    return {
        **row,
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
            "instances": collector_instances(name) if row["channel"] == "push" else [],
            "incremental": _files_view(runtime_inc),
            "reportUrl": "%s/current/html/index.html" % base if row["hasReport"] else None,
            "xmlUrl": "%s/current/jacoco.xml" % base,
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
