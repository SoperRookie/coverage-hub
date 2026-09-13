"""数据访问。只收发 dict，ORM 对象不出这个模块。

每个函数自开自关一个会话（一个短事务）。调用方大多在采集线程、收集端线程或
请求线程池里，把 Session 带出去只会换来 DetachedInstanceError 和跨线程共享
会话的坑。
"""

import os
from datetime import datetime

from sqlalchemy import select

from ..errors import CovhubError, ServiceNotFound
from .engine import session_scope
from .models import Archive, Break, Service, ServiceState, Snapshot


# ---- 服务配置 ----

def _get(session, name):
    svc = session.scalar(select(Service).where(Service.name == name))
    if svc is None:
        raise ServiceNotFound("配置里没有名为 %r 的服务" % name)
    return svc


def list_services(cfg=None):
    base = (cfg or {}).get("baseDir")
    with session_scope() as s:
        rows = s.scalars(select(Service).order_by(Service.id)).all()
        return [r.to_dict(base) for r in rows]


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
        svc = Service()
        svc.apply(fields)
        svc.state = ServiceState()
        s.add(svc)
        s.flush()
        return svc.to_dict()


def replace_service(name, fields):
    """整份替换（PUT）：没给的可选字段清空。"""
    with session_scope() as s:
        svc = _get(s, name)
        blank = {key: None for key in ("version", "address", "port", "bindAddress",
                                       "classDumpDir", "sourceEncoding", "dumpRetry")}
        blank.update({key: [] for key in ("includes", "excludes", "classfiles",
                                          "sourcefiles", "reportExcludes")})
        blank.update(fields)
        blank.pop("name", None)
        svc.apply(blank)
        s.flush()
        return svc.to_dict()


def update_service(name, fields):
    """局部更新（PATCH / retarget）：只动给到的键。"""
    with session_scope() as s:
        svc = _get(s, name)
        fields = dict(fields)
        fields.pop("name", None)
        svc.apply(fields)
        s.flush()
        return svc.to_dict()


def remove_service(name):
    with session_scope() as s:
        svc = _get(s, name)
        s.delete(svc)


def upsert_service(fields, overwrite=False):
    """import 用：返回 added / updated / skipped。"""
    with session_scope() as s:
        svc = s.scalar(select(Service).where(Service.name == fields["name"]))
        if svc is None:
            svc = Service()
            svc.apply(fields)
            svc.state = ServiceState()
            s.add(svc)
            return "added"
        if not overwrite:
            return "skipped"
        svc.apply({k: v for k, v in fields.items() if k != "name"})
        return "updated"


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
        )
        s.add(row)
        s.flush()
        return _snapshot_dict(row)


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
