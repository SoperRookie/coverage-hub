"""把旧的 targets.yaml（services 段）和 data/<svc>/state.json 导进数据库。

幂等：服务按 name 判断，已存在默认跳过；快照按 (时刻, 类型, 计数) 去重，
归档按目录名去重，断代按 (时刻, 类型) 去重。中途失败可以直接重跑。
默认不动磁盘上的任何文件。
"""

import json
import os
from datetime import datetime

from sqlalchemy import select

from ..config import read_config_file
from ..layout import svc_dir
from ..logbuf import log
from ..schemas import ServiceSpec
from . import repo
from .engine import session_scope
from .models import Archive, Break, Service, ServiceState, Snapshot


def import_services(config_path, dry_run=False, overwrite=False):
    """返回 {name: added|updated|skipped|invalid}。"""
    raw = read_config_file(config_path)
    result = {}
    for item in raw.get("services") or []:
        name = item.get("name", "?")
        try:
            # 存原文：相对路径不展开，整个目录搬家后照样成立
            fields = ServiceSpec(**item).to_fields()
        except Exception as exc:
            log("  ! %s：配置不合法，跳过 —— %s" % (name, _brief(exc)))
            result[name] = "invalid"
            continue
        if dry_run:
            exists = name in repo.list_service_names()
            result[name] = ("updated" if overwrite else "skipped") if exists else "added"
        else:
            result[name] = repo.upsert_service(fields, overwrite=overwrite)
        log("  %s：%s" % (name, {"added": "已导入", "updated": "已更新",
                                  "skipped": "已存在，跳过（--overwrite 可覆盖）"}[result[name]]))
    return result


def _brief(exc):
    text = str(exc)
    return text.splitlines()[0] if text else type(exc).__name__


# ---- 覆盖率历史：state.json + versions/*/manifest.json ----

def import_state(cfg, name, dry_run=False):
    """把 data/<name>/state.json 与各归档的 manifest.json 导进库。返回各类计数。

    归档行以 manifest.json 为准（sealedAt / sealedBy / 指纹 / 体检结论都在里面），
    state.json 的 versions[] 只在 manifest 缺 matchRate 时兜底。老版本的 manifest
    可能没有 fingerprint / execCount 这些键，都按缺省处理。
    """
    root = svc_dir(cfg, {"name": name})
    counts = {"snapshots": 0, "archives": 0, "breaks": 0, "skipped": 0}
    state = _read_json(os.path.join(root, "state.json")) or {}

    entries = list(state.get("history") or [])
    # 归档的 summary 也可能不在 history 里（history 曾截断到 500 条），一并算进候选
    manifests = _manifests(root)
    for _dirname, m in manifests:
        if isinstance(m.get("summary"), dict):
            entries.append(m["summary"])
    entries.sort(key=lambda e: e.get("at") or "")

    versions_hint = {v.get("at"): v.get("matchRate")
                     for v in (state.get("versions") or []) if isinstance(v, dict)}

    with session_scope() as s:
        svc = s.scalar(select(Service).where(Service.name == name))
        if svc is None:
            log("  ! %s：库里没有这个服务，先导入服务配置" % name)
            return counts
        seen = set()
        for e in entries:
            key = _entry_key(e)
            if key is None or key in seen:
                continue
            seen.add(key)
            if _find_snapshot(s, svc.id, e) is not None:
                counts["skipped"] += 1
                continue
            counts["snapshots"] += 1
            if not dry_run:
                s.add(Snapshot(
                    service_id=svc.id, at=datetime.fromisoformat(e["at"]),
                    kind=e.get("kind", "dump"), version=_str(e.get("version")),
                    instruction=float(e.get("instruction") or 0),
                    branch=float(e.get("branch") or 0),
                    covered=int(e.get("covered") or 0), total=int(e.get("total") or 0),
                    classes_hit=int(e.get("classesHit") or 0),
                    classes_total=int(e.get("classesTotal") or 0),
                    reason=e.get("reason"), session_start=e.get("sessionStart"),
                    match_rate=e.get("matchRate", versions_hint.get(e.get("at")))))
        s.flush()

        for dirname, m in manifests:
            archive_dir = "versions/" + dirname
            if s.scalar(select(Archive.id).where(Archive.service_id == svc.id,
                                                 Archive.archive_dir == archive_dir)):
                counts["skipped"] += 1
                continue
            summary = m.get("summary") if isinstance(m.get("summary"), dict) else {}
            sealed_at = m.get("sealedAt") or summary.get("at")
            if not sealed_at:
                log("  ! %s/%s：manifest 没有 sealedAt，跳过" % (name, archive_dir))
                continue
            snap = _find_snapshot(s, svc.id, summary or {"at": sealed_at})
            if snap is None:
                log("  ! %s/%s：找不到对应的快照记录，跳过" % (name, archive_dir))
                continue
            counts["archives"] += 1
            if dry_run:
                continue
            match_rate = m.get("matchRate", versions_hint.get(sealed_at))
            s.add(Archive(
                service_id=svc.id, snapshot_id=snap.id,
                version=_str(m.get("version")) or dirname, archive_dir=archive_dir,
                sealed_at=datetime.fromisoformat(sealed_at),
                sealed_by=m.get("sealedBy") or "predeploy",
                fingerprint=m.get("fingerprint"), exec_count=int(m.get("execCount") or 0),
                match_rate=match_rate, health_verdict=m.get("healthVerdict"),
                merged=m.get("merged")))
            if match_rate is not None and snap.match_rate is None:
                snap.match_rate = match_rate
        s.flush()

        for b in state.get("breaks") or []:
            if not isinstance(b, dict) or not b.get("at"):
                continue
            kind = "restart" if b.get("sealedAs") else "mixed-versions"
            at = datetime.fromisoformat(b["at"])
            if s.scalar(select(Break.id).where(Break.service_id == svc.id, Break.at == at,
                                               Break.kind == kind)):
                counts["skipped"] += 1
                continue
            counts["breaks"] += 1
            if dry_run:
                continue
            archive_id = None
            if b.get("sealedAs"):
                archive_id = s.scalar(select(Archive.id).where(
                    Archive.service_id == svc.id,
                    Archive.archive_dir == "versions/" + b["sealedAs"]))
            s.add(Break(service_id=svc.id, at=at, kind=kind,
                        from_session=b.get("from"), to_session=b.get("to"),
                        sealed_as=b.get("sealedAs"), archive_id=archive_id,
                        instances=b.get("instances")))

        if not dry_run:
            if svc.state is None:
                svc.state = ServiceState()
            svc.state.session_start = state.get("sessionStart")
            svc.state.push_mixed = bool(state.get("pushMixed"))
    return counts


def _read_json(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError) as exc:
        log("  ! %s 读取失败，跳过：%s" % (path, exc))
        return None


def _manifests(root):
    out = []
    vdir = os.path.join(root, "versions")
    if not os.path.isdir(vdir):
        return out
    for dirname in sorted(os.listdir(vdir)):
        m = _read_json(os.path.join(vdir, dirname, "manifest.json"))
        if isinstance(m, dict):
            out.append((dirname, m))
    return out


def _str(value):
    return None if value is None else str(value)


def _entry_key(e):
    if not isinstance(e, dict) or not e.get("at"):
        return None
    return (e["at"], e.get("kind", "dump"), e.get("covered"), e.get("total"))


def _find_snapshot(s, sid, e):
    q = select(Snapshot).where(Snapshot.service_id == sid,
                               Snapshot.at == datetime.fromisoformat(e["at"]))
    if e.get("kind"):
        q = q.where(Snapshot.kind == e["kind"])
    if e.get("covered") is not None:
        q = q.where(Snapshot.covered == int(e["covered"]))
    return s.scalar(q.order_by(Snapshot.id).limit(1))
