"""push 收集端里不起 JVM 也能验的几条：混版本判定、匹配不到服务的连接怎么释放。"""

import socket
import threading

import pytest

from covhub import jacoco
from covhub.collector import PushCollector
from covhub.errors import CovhubError


def _conn(cid, service, classes, sock=None, rfile=None):
    return {"id": cid, "peer": "10.0.0.%d:1" % cid, "sessionid": service, "service": service,
            "since": "2026-10-08T10:00:00", "last": None, "sock": sock, "rfile": rfile,
            "wfile": None, "sessionStart": None, "classes": classes}


def test_mixed_versions_compares_ids_by_class_name():
    """exec 里只有执行过的类：两个副本各跑各的请求，集合不同不算混版本；
    同名类 id 不同才是真换了版本。"""
    pc = PushCollector(lambda: {})
    pc.conns[1] = _conn(1, "svc", {"a/Foo": 1, "a/Bar": 2})
    pc.conns[2] = _conn(2, "svc", {"a/Foo": 1, "a/Baz": 3})          # 各有对方没跑到的类
    assert pc.mixed_versions("svc") is False

    pc.conns[3] = _conn(3, "svc", {"a/Foo": 99})                      # Foo 是另一份 class
    assert pc.mixed_versions("svc") is True

    pc.conns[4] = _conn(4, "other", {"a/Foo": 7})                     # 别的服务不参与
    assert pc.mixed_versions("other") is False


def test_unmatched_connection_released_when_peer_closes():
    """sessionid 匹配不到服务的连接：守着它读到对方断开再释放，不泄漏也不立刻踢掉。"""
    pc = PushCollector(lambda: {})
    ours, theirs = socket.socketpair()
    try:
        rfile = ours.makefile("rb")
        pc.conns[1] = _conn(1, None, {}, sock=ours, rfile=rfile)
        t = threading.Thread(target=pc._park, args=(1, ours, rfile), daemon=True)
        t.start()
        t.join(0.3)
        assert t.is_alive() and 1 in pc.conns          # 对方还在，连接还登记着
        theirs.close()                                  # agent 到 idle 自己断开
        t.join(5)
        assert not t.is_alive() and 1 not in pc.conns
    finally:
        theirs.close()
        ours.close()


def test_class_file_ids_rejects_missing_path():
    """classfiles 指向不存在的路径：直接报业务错误，不交给 classinfo 去拿一个非零退出。"""
    with pytest.raises(CovhubError, match="不存在"):
        jacoco.class_file_ids({}, ["/no/such/dir-xyz"])
