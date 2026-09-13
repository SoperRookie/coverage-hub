"""数据访问。只收发 dict，ORM 对象不出这个模块。

每个函数自开自关一个会话（一个短事务）。调用方大多在采集线程、收集端线程或
请求线程池里，把 Session 带出去只会换来 DetachedInstanceError 和跨线程共享
会话的坑。
"""

from sqlalchemy import select

from ..errors import CovhubError, ServiceNotFound
from .engine import session_scope
from .models import Service, ServiceState


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
