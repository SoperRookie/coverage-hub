"""数据访问。只收发 dict，ORM 对象不出这个模块。

每个函数自开自关一个会话（一个短事务）。调用方大多在采集线程、收集端线程或
请求线程池里，把 Session 带出去只会换来 DetachedInstanceError 和跨线程共享
会话的坑。
"""

import os
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..errors import CovhubError, ServiceNotFound
from .engine import session_scope
from .models import Archive, Break, Diff, Project, Service, ServiceState, Snapshot, UnitReport


# ---- 服务配置 ----

def _get(session, name):
    svc = session.scalar(select(Service).options(selectinload(Service.project))
                         .where(Service.name == name))
    if svc is None:
        raise ServiceNotFound("配置里没有名为 %r 的服务" % name)
    return svc


def _resolve_project(session, fields):
    """fields 里的 project 是名字；换成 project_id 赋给 Service。给 None 就是解绑。"""
    if "project" not in fields:
        return None
    name = fields.pop("project")
    if name is None:
        return None
    proj = session.scalar(select(Project).where(Project.name == name))
    if proj is None:
        raise CovhubError("没有名为 %r 的项目，先 covhub project add" % name)
    return proj.id


def list_services(cfg=None, project=None):
    base = (cfg or {}).get("baseDir")
    with session_scope() as s:
        q = select(Service).options(selectinload(Service.project)).order_by(Service.id)
        if project is not None:
            q = q.join(Project).where(Project.name == project)
        return [r.to_dict(base) for r in s.scalars(q).all()]


def list_service_names():
    with session_scope() as s:
        return list(s.scalars(select(Service.name).order_by(Service.id)).all())


def get_service(name, cfg=None):
    base = (cfg or {}).get("baseDir")
    with session_scope() as s:
        return _get(s, name).to_dict(base)


def add_service(fields):
    """fields 是 ServiceSpec.to_fields() 的结果（camelCase）。同名已存在则报错。"""
    with session_scope() as s:
        if s.scalar(select(Service.id).where(Service.name == fields["name"])) is not None:
            raise CovhubError("服务 %r 已存在，要改用 service update" % fields["name"])
        fields = dict(fields)
        svc = Service()
        if "project" in fields:
            svc.project_id = _resolve_project(s, fields)
        svc.apply(fields)
        svc.state = ServiceState()
        s.add(svc)
        s.flush()
        s.refresh(svc)
        return svc.to_dict()


def replace_service(name, fields):
    """整份替换（PUT）：没给的可选字段清空。"""
    with session_scope() as s:
        svc = _get(s, name)
        blank = {key: None for key in ("version", "address", "port", "bindAddress",
                                       "classDumpDir", "sourceEncoding", "dumpRetry", "project")}
        blank.update({key: [] for key in ("includes", "excludes", "classfiles",
                                          "sourcefiles", "reportExcludes")})
        blank.update(fields)
        blank.pop("name", None)
        svc.project_id = _resolve_project(s, blank)
        svc.apply(blank)
        s.flush()
        s.refresh(svc)
        return svc.to_dict()


def update_service(name, fields):
    """局部更新（PATCH / retarget）：只动给到的键。"""
    with session_scope() as s:
        svc = _get(s, name)
        fields = dict(fields)
        fields.pop("name", None)
        if "project" in fields:
            svc.project_id = _resolve_project(s, fields)
        svc.apply(fields)
        s.flush()
        s.refresh(svc)
        return svc.to_dict()


def remove_service(name):
    with session_scope() as s:
        svc = _get(s, name)
        s.delete(svc)


def upsert_service(fields, overwrite=False):
    """import 用：返回 added / updated / skipped。"""
    with session_scope() as s:
        fields = dict(fields)
        svc = s.scalar(select(Service).where(Service.name == fields["name"]))
        if svc is None:
            svc = Service()
            if "project" in fields:
                svc.project_id = _resolve_project(s, fields)
            svc.apply(fields)
            svc.state = ServiceState()
            s.add(svc)
            return "added"
        if not overwrite:
            return "skipped"
        if "project" in fields:
            svc.project_id = _resolve_project(s, fields)
        svc.apply({k: v for k, v in fields.items() if k != "name"})
        return "updated"


# ---- 项目 ----

def _get_project(session, name):
    proj = session.scalar(select(Project).where(Project.name == name))
    if proj is None:
        raise ServiceNotFound("没有名为 %r 的项目" % name)
    return proj


def list_projects():
    with session_scope() as s:
        rows = s.scalars(select(Project).options(selectinload(Project.services))
                         .order_by(Project.id)).all()
        out = []
        for r in rows:
            d = r.to_dict()
            d["services"] = [svc.name for svc in sorted(r.services, key=lambda x: x.id)]
            out.append(d)
        return out


def get_project(name):
    with session_scope() as s:
        proj = _get_project(s, name)
        d = proj.to_dict()
        d["services"] = [svc.name for svc in sorted(proj.services, key=lambda x: x.id)]
        return d


def add_project(fields):
    with session_scope() as s:
        if s.scalar(select(Project.id).where(Project.name == fields["name"])) is not None:
            raise CovhubError("项目 %r 已存在" % fields["name"])
        proj = Project(**fields)
        s.add(proj)
        s.flush()
        d = proj.to_dict()
        d["services"] = []
        return d


def update_project(name, fields):
    with session_scope() as s:
        proj = _get_project(s, name)
        for key, value in fields.items():
            setattr(proj, key, value)
        s.flush()
        d = proj.to_dict()
        d["services"] = [svc.name for svc in proj.services]
        return d


def remove_project(name):
    """只删项目；服务的 project_id 由外键 SET NULL（SQLite 靠 PRAGMA foreign_keys=ON）。"""
    with session_scope() as s:
        proj = _get_project(s, name)
        for svc in proj.services:          # 不依赖数据库的级联行为，跨方言更稳
            svc.project_id = None
        s.delete(proj)


# ---- 覆盖率历史（原 state.json）----

def _iso(dt):
    return dt.isoformat(timespec="seconds") if dt else None


def _parse_at(value):
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def _snapshot_dict(row):
    d = {
        "id": row.id, "at": _iso(row.at), "kind": row.kind, "version": row.version,
        "instruction": row.instruction, "branch": row.branch,
        "covered": row.covered, "total": row.total,
        "classesHit": row.classes_hit, "classesTotal": row.classes_total,
    }
    if row.reason is not None:
        d["reason"] = row.reason
    if row.session_start is not None:
        d["sessionStart"] = row.session_start
    if row.match_rate is not None:
        d["matchRate"] = row.match_rate
    if row.inc_total is not None:
        d["incCovered"], d["incTotal"], d["incPct"] = row.inc_covered, row.inc_total, row.inc_pct
    return d


def _break_dict(row):
    # 还原成 state.json 时代的两种形态，看板与 diagnose 的输出不用改
    if row.kind == "restart":
        return {"at": _iso(row.at), "from": row.from_session, "to": row.to_session,
                "sealedAs": row.sealed_as}
    return {"at": _iso(row.at), "reason": row.kind, "instances": row.instances}


def latest(name):
    with session_scope() as s:
        sid = _get(s, name).id
        row = s.scalar(select(Snapshot).where(Snapshot.service_id == sid)
                       .order_by(Snapshot.id.desc()).limit(1))
        return _snapshot_dict(row) if row else None


def history(name, limit=40):
    """最近 limit 条，按时间正序（看板画趋势用）。"""
    with session_scope() as s:
        sid = _get(s, name).id
        rows = s.scalars(select(Snapshot).where(Snapshot.service_id == sid)
                         .order_by(Snapshot.id.desc()).limit(limit)).all()
        return [_snapshot_dict(r) for r in reversed(rows)]


def versions(name, limit=10, sealed_by="predeploy"):
    """已结算的版本，按结算时间正序。sealed_by=None 则连重启封存的也列出来。"""
    with session_scope() as s:
        sid = _get(s, name).id
        q = (select(Archive, Snapshot).join(Snapshot, Archive.snapshot_id == Snapshot.id)
             .where(Archive.service_id == sid))
        if sealed_by:
            q = q.where(Archive.sealed_by == sealed_by)
        rows = s.execute(q.order_by(Archive.id.desc()).limit(limit)).all()
        out = []
        for archive, snap in reversed(rows):
            d = _snapshot_dict(snap)
            d.update({"version": archive.version, "dir": os.path.basename(archive.archive_dir),
                      "sealedBy": archive.sealed_by, "sealedAt": _iso(archive.sealed_at)})
            if archive.match_rate is not None:
                d["matchRate"] = archive.match_rate
            out.append(d)
        return out


def archive_by_dir(name, archive_dir):
    """某个归档目录对应的结算快照（含归档元数据），给「看历史版本」用。"""
    with session_scope() as s:
        sid = _get(s, name).id
        row = s.execute(select(Archive, Snapshot).join(Snapshot, Archive.snapshot_id == Snapshot.id)
                        .where(Archive.service_id == sid, Archive.archive_dir == archive_dir)).first()
        if not row:
            return None
        archive, snap = row
        d = _snapshot_dict(snap)
        d.update({"version": archive.version, "dir": os.path.basename(archive.archive_dir),
                  "sealedBy": archive.sealed_by, "sealedAt": _iso(archive.sealed_at),
                  "execCount": archive.exec_count, "healthVerdict": archive.health_verdict})
        if archive.match_rate is not None:
            d["matchRate"] = archive.match_rate
        return d


def versions_since(name, since=None, sealed_by="predeploy"):
    """时间范围内的已结算版本（正序），报表用。since 是 datetime 或 None（不限）。"""
    with session_scope() as s:
        sid = _get(s, name).id
        q = (select(Archive, Snapshot).join(Snapshot, Archive.snapshot_id == Snapshot.id)
             .where(Archive.service_id == sid))
        if sealed_by:
            q = q.where(Archive.sealed_by == sealed_by)
        if since is not None:
            q = q.where(Archive.sealed_at >= since)
        out = []
        # 按结算时刻排，不按入库顺序：导入的旧归档 id 可能比新的大
        for archive, snap in s.execute(q.order_by(Archive.sealed_at, Archive.id)).all():
            d = _snapshot_dict(snap)
            d.update({"version": archive.version, "dir": os.path.basename(archive.archive_dir),
                      "sealedBy": archive.sealed_by, "sealedAt": _iso(archive.sealed_at)})
            if archive.match_rate is not None:
                d["matchRate"] = archive.match_rate
            out.append(d)
        return out


def breaks(name, limit=50):
    with session_scope() as s:
        sid = _get(s, name).id
        rows = s.scalars(select(Break).where(Break.service_id == sid)
                         .order_by(Break.id.desc()).limit(limit)).all()
        return [_break_dict(r) for r in reversed(rows)]


def get_state(name):
    with session_scope() as s:
        st = _get(s, name).state
        return {"sessionStart": st.session_start if st else None,
                "pushMixed": bool(st.push_mixed) if st else False}


def _state_row(session, svc):
    if svc.state is None:
        svc.state = ServiceState()
        session.flush()
    return svc.state


def set_session_start(name, value):
    with session_scope() as s:
        _state_row(s, _get(s, name)).session_start = value


def set_online(name, online):
    """采集线程每轮探活后写入；看板读这里，请求路径上不做 TCP 探活。"""
    with session_scope() as s:
        st = _state_row(s, _get(s, name))
        st.online = bool(online)
        st.online_at = datetime.now().replace(microsecond=0)


def get_online(name):
    with session_scope() as s:
        st = _get(s, name).state
        if st is None or st.online is None:
            return None, None
        return bool(st.online), _iso(st.online_at)


def add_snapshot(name, entry):
    """entry 是 record() 拼好的 dict（at 为 ISO 字符串），返回带 id 的同形态 dict。"""
    with session_scope() as s:
        svc = _get(s, name)
        row = Snapshot(
            service_id=svc.id, at=_parse_at(entry["at"]), kind=entry["kind"],
            version=entry.get("version"),
            instruction=entry["instruction"], branch=entry["branch"],
            covered=entry["covered"], total=entry["total"],
            classes_hit=entry["classesHit"], classes_total=entry["classesTotal"],
            reason=entry.get("reason"), session_start=entry.get("sessionStart"),
            inc_covered=entry.get("incCovered"), inc_total=entry.get("incTotal"),
            inc_pct=entry.get("incPct"),
        )
        s.add(row)
        s.flush()
        return _snapshot_dict(row)


def update_snapshot_incremental(snapshot_id, covered, total, pct):
    """diff 晚于快照到达时回写这一条（只改 DB 行，磁盘归档不动）。"""
    with session_scope() as s:
        snap = s.get(Snapshot, snapshot_id)
        if snap is not None:
            snap.inc_covered, snap.inc_total, snap.inc_pct = covered, total, pct


def latest_snapshot_of_version(name, version):
    """某版本最新的一条快照，及它对应的归档目录（已归档时）。"""
    with session_scope() as s:
        sid = _get(s, name).id
        row = s.scalar(select(Snapshot).where(Snapshot.service_id == sid, Snapshot.version == version)
                       .order_by(Snapshot.id.desc()).limit(1))
        if row is None:
            return None, None
        archive = s.scalar(select(Archive.archive_dir).where(Archive.snapshot_id == row.id))
        return _snapshot_dict(row), archive


def finish_archive(name, snapshot_id, version, archive_dir, sealed_by, sealed_at,
                   fingerprint=None, exec_count=0, match_rate=None, health_verdict=None,
                   merged=None):
    """归档落盘之后记一行 archives、把体检结论写回对应快照、清掉会话基线。

    三件事在同一个事务里：它们描述的是同一个「周期结束」事实。
    """
    with session_scope() as s:
        svc = _get(s, name)
        row = Archive(service_id=svc.id, snapshot_id=snapshot_id, version=version,
                      archive_dir=archive_dir, sealed_at=_parse_at(sealed_at),
                      sealed_by=sealed_by, fingerprint=fingerprint, exec_count=exec_count,
                      match_rate=match_rate, health_verdict=health_verdict, merged=merged)
        s.add(row)
        if match_rate is not None:
            snap = s.get(Snapshot, snapshot_id)
            if snap is not None:
                snap.match_rate = match_rate
        # 新周期从零开始：会话基线作废，等下一次采集重新认
        _state_row(s, svc).session_start = None
        s.flush()
        return row.id


def add_break(name, at, from_session, to_session, sealed_as):
    """pull 通道的断代（进程重启，已封存）。sealed_as 能对上归档目录就把外键也填上。"""
    with session_scope() as s:
        svc = _get(s, name)
        archive_id = s.scalar(select(Archive.id).where(
            Archive.service_id == svc.id,
            Archive.archive_dir == "versions/" + sealed_as)) if sealed_as else None
        row = Break(service_id=svc.id, at=_parse_at(at), kind="restart",
                    from_session=from_session, to_session=to_session,
                    sealed_as=sealed_as, archive_id=archive_id)
        s.add(row)
        s.flush()
        return _break_dict(row)


def record_push_mixed(name, mixed, instances, at=None):
    """push 通道的混版本状态翻转。进入混版本时记一条 break；解除时只改标记。"""
    with session_scope() as s:
        svc = _get(s, name)
        st = _state_row(s, svc)
        st.push_mixed = bool(mixed)
        if not mixed:
            return None
        row = Break(service_id=svc.id, at=_parse_at(at) if at else datetime.now().replace(microsecond=0),
                    kind="mixed-versions", instances=instances)
        s.add(row)
        s.flush()
        return _break_dict(row)


# ---- 单测报告（构建流水线传上来的 jacoco.xml）----

def _unit_dict(row):
    d = {"id": row.id, "version": row.version, "at": _iso(row.at),
         "instruction": row.instruction, "branch": row.branch, "line": row.line,
         "covered": row.covered, "total": row.total,
         "linesCovered": row.lines_covered, "linesTotal": row.lines_total,
         "classesHit": row.classes_hit, "classesTotal": row.classes_total,
         "xmlPath": row.xml_path}
    if row.inc_total is not None:
        d["incCovered"], d["incTotal"], d["incPct"] = row.inc_covered, row.inc_total, row.inc_pct
    return d


def upsert_unit_report(name, version, summary, xml_path, incremental=None):
    """同版本重传覆盖。summary 是 unit.parse 出的计数器 dict。"""
    with session_scope() as s:
        svc = _get(s, name)
        row = s.scalar(select(UnitReport).where(UnitReport.service_id == svc.id,
                                                UnitReport.version == version))
        if row is None:
            row = UnitReport(service_id=svc.id, version=version)
            s.add(row)
        row.at = datetime.now().replace(microsecond=0)
        for key in ("instruction", "branch", "line", "covered", "total", "lines_covered",
                    "lines_total", "classes_hit", "classes_total"):
            setattr(row, key, summary[key])
        row.xml_path = xml_path
        _apply_incremental(row, incremental)
        s.flush()
        return _unit_dict(row)


def _apply_incremental(row, incremental):
    if incremental is None:
        row.inc_covered = row.inc_total = row.inc_pct = None
    else:
        row.inc_covered, row.inc_total = incremental["covered"], incremental["total"]
        row.inc_pct = incremental["pct"]


def update_unit_incremental(name, version, incremental):
    with session_scope() as s:
        svc = _get(s, name)
        row = s.scalar(select(UnitReport).where(UnitReport.service_id == svc.id,
                                                UnitReport.version == version))
        if row is None:
            return None
        _apply_incremental(row, incremental)
        s.flush()
        return _unit_dict(row)


def unit_report(name, version):
    with session_scope() as s:
        svc = _get(s, name)
        row = s.scalar(select(UnitReport).where(UnitReport.service_id == svc.id,
                                                UnitReport.version == version))
        return _unit_dict(row) if row else None


def latest_unit_report(name):
    with session_scope() as s:
        sid = _get(s, name).id
        row = s.scalar(select(UnitReport).where(UnitReport.service_id == sid)
                       .order_by(UnitReport.id.desc()).limit(1))
        return _unit_dict(row) if row else None


def unit_reports_since(name, since=None):
    with session_scope() as s:
        sid = _get(s, name).id
        q = select(UnitReport).where(UnitReport.service_id == sid)
        if since is not None:
            q = q.where(UnitReport.at >= since)
        return [_unit_dict(r) for r in s.scalars(q.order_by(UnitReport.at, UnitReport.id)).all()]


def unit_history(name, limit=40):
    with session_scope() as s:
        sid = _get(s, name).id
        rows = s.scalars(select(UnitReport).where(UnitReport.service_id == sid)
                         .order_by(UnitReport.id.desc()).limit(limit)).all()
        return [_unit_dict(r) for r in reversed(rows)]


# ---- diff（流水线传上来的 git diff 摘要）----

def _diff_dict(row):
    return {"id": row.id, "version": row.version, "base": row.base, "head": row.head,
            "at": _iso(row.at), "files": row.files, "addedLines": row.added_lines}


def upsert_diff(name, version, base, head, files, added_lines):
    with session_scope() as s:
        svc = _get(s, name)
        row = s.scalar(select(Diff).where(Diff.service_id == svc.id, Diff.version == version))
        if row is None:
            row = Diff(service_id=svc.id, version=version)
            s.add(row)
        row.base, row.head = base, head
        row.at = datetime.now().replace(microsecond=0)
        row.files, row.added_lines = files, added_lines
        s.flush()
        return _diff_dict(row)


def get_diff(name, version):
    with session_scope() as s:
        svc = _get(s, name)
        row = s.scalar(select(Diff).where(Diff.service_id == svc.id, Diff.version == version))
        return _diff_dict(row) if row else None


def latest_diff(name):
    with session_scope() as s:
        sid = _get(s, name).id
        row = s.scalar(select(Diff).where(Diff.service_id == sid).order_by(Diff.id.desc()).limit(1))
        return _diff_dict(row) if row else None
