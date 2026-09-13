"""引擎与会话的生命周期。整个进程只有一个引擎，由 configure() 按配置建起来。"""

import contextlib

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ..errors import ConfigError

_engine = None
_Session = None
_url = None


def configure(url):
    """按 URL 建引擎。同一进程里 URL 不变时是幂等的；变了就换掉（只有测试会这么干）。"""
    global _engine, _Session, _url
    if _engine is not None and url == _url:
        return _engine
    dispose()

    kwargs = {"pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        # 采集线程、收集端线程、请求线程池都会碰库；SQLite 的连接默认绑线程。
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if ":memory:" in url or url.endswith("sqlite://"):
            kwargs["poolclass"] = StaticPool     # 内存库：所有会话共用同一条连接
    else:
        kwargs["pool_recycle"] = 3600            # 别被 MySQL 的 wait_timeout 掐掉
    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")      # SQLite 默认不检查外键
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    _engine, _url = engine, url
    _Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return engine


def get_engine():
    if _engine is None:
        raise ConfigError("数据库尚未初始化 —— 入口处应先 load_runtime()")
    return _engine


def current_url():
    return _url


@contextlib.contextmanager
def session_scope():
    """一个短事务：正常结束 commit，异常 rollback，最后关闭。"""
    if _Session is None:
        raise ConfigError("数据库尚未初始化 —— 入口处应先 load_runtime()")
    session = _Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def dispose():
    global _engine, _Session, _url
    if _engine is not None:
        _engine.dispose()
    _engine = _Session = _url = None
