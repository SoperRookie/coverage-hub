"""push 收集端里不起 JVM 也能验的几条：混版本判定、匹配不到服务的连接怎么释放。"""

import os
import socket
import threading

import pytest

from covhub import collector as col
from covhub import config, jacoco, watch
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


def test_drop_remembers_jvm_and_twin_is_retired():
    """掉线的 JVM 按会话起点记在 dropped 里；同一个 JVM 重连上来时旧记录被清掉、不算掉线。"""
    pc = PushCollector(lambda: {})
    pc.conns[1] = _conn(1, "svc", {}) | {"sessionStart": 1000, "peer": "10.0.0.8:40001"}
    pc.conns[2] = _conn(2, "svc", {}) | {"sessionStart": 2000, "peer": "10.0.0.9:40002"}
    pc.drop(2, "timed out")
    assert pc.dropped == {"svc": {2000}}

    pc.conns[3] = _conn(3, "svc", {}) | {"sessionStart": 1000, "peer": "10.0.0.8:40777"}
    pc._retire_twin(3, "svc", "10.0.0.8", 1000)
    assert set(pc.conns) == {3} and pc.dropped == {"svc": {2000}}     # 旧连接没了，也没记成掉线


def _cfg(tmp_path):
    return {"dataDir": str(tmp_path), "services": [{"name": "svc", "channel": "push"}]}


def _fake_dump(calls, answers):
    """替身 remote_dump：按 wfile 认连接，记下每次调用的 reset 标志。"""
    def remote_dump(rfile, wfile, reset=False):
        calls.append((wfile, reset))
        ans = answers[wfile]
        if isinstance(ans, Exception):
            raise ans
        return ans
    return remote_dump


def test_reset_marks_absent_instances_and_their_handshake_is_discarded(tmp_path, monkeypatch):
    """结算清零时没应答的实例和早先掉线的实例，重连时握手带回的是上一周期的数据：丢弃并清零。"""
    cfg = _cfg(tmp_path)
    pc = PushCollector(lambda: cfg)
    a_ours, a_theirs = socket.socketpair()
    b_ours, b_theirs = socket.socketpair()
    c_ours, c_theirs = socket.socketpair()
    socks = [a_ours, a_theirs, b_ours, b_theirs, c_ours, c_theirs]
    try:
        files = {s: (s.makefile("rb"), s.makefile("wb")) for s in (a_ours, b_ours)}
        for cid, (sock, start) in enumerate(((a_ours, 1000), (b_ours, 2000)), start=1):
            pc.conns[cid] = _conn(cid, "svc", {}, sock=sock, rfile=files[sock][0]) | {
                "wfile": files[sock][1], "sessionStart": start}
        pc.seq = 2
        pc.dropped["svc"] = {3000}                      # 早先掉线、还没回来的
        calls = []
        answers = {files[a_ours][1]: ([("svc", 1000, 1)], []),
                   files[b_ours][1]: TimeoutError("timed out")}
        monkeypatch.setattr(col, "remote_dump", _fake_dump(calls, answers))

        assert pc.dump_service(cfg, cfg["services"][0], reset=True) == 1
        assert pc.missed_reset == {"svc": {2000, 3000}} and "svc" not in pc.dropped
        assert 2 not in pc.conns                        # 没应答的已丢弃

        # 错过清零的 JVM（会话起点 2000）重连：握手数据不落盘，紧接着一次 reset=True 清零。
        # _claim 自己 makefile，替身按 wfile 认不出它，所以按「不是 a 的」来答
        def by_conn(rfile, wfile, reset=False):
            calls.append((wfile, reset))
            if wfile is files[a_ours][1]:
                return answers[wfile]
            return ([("svc", 2000, 2)], [(7, "a/Foo", [True])])
        monkeypatch.setattr(col, "remote_dump", by_conn)
        before = set(os.listdir(os.path.join(tmp_path, "svc", "exec")))
        n_before = len(calls)
        pc._claim(c_ours, ("10.0.0.7", 5555))
        assert [r for _, r in calls[n_before:]] == [False, True]
        assert set(os.listdir(os.path.join(tmp_path, "svc", "exec"))) == before
        assert pc.missed_reset == {"svc": {3000}}
        new = pc.conns[pc.seq]
        assert new["service"] == "svc" and new["sessionStart"] is None and new["classes"] == {}
    finally:
        for s in socks:
            s.close()


def test_store_does_not_overwrite_same_second_file(tmp_path):
    cfg = _cfg(tmp_path)
    pc = PushCollector(lambda: cfg)
    pc.conns[1] = _conn(1, "svc", {})
    p1 = pc._store(cfg, cfg["services"][0], 1, [("svc", 1, 2)], [])
    p2 = pc._store(cfg, cfg["services"][0], 1, [("svc", 1, 2)], [])
    assert p1 != p2 and os.path.isfile(p1) and os.path.isfile(p2)
    assert pc.conns[1]["sessionStart"] == 1             # 每次取数刷新会话起点


def test_watch_skips_push_service_without_collector(monkeypatch):
    """单独的 covhub watch 进程里探不到 push 实例：不能把 online=False 写进库。"""
    written = []
    monkeypatch.setattr(watch.repo, "set_online", lambda *a: written.append(a))
    monkeypatch.setattr(col, "_COLLECTOR", None)
    watch.watch_once({"services": [{"name": "p", "channel": "push"}]})
    assert written == []


def test_serve_interval_override_reaches_config(tmp_path):
    """serve --interval 要让每次重读的配置都看到：agent-opts 的 idle 按它算。"""
    path = tmp_path / "covhub.json"
    path.write_text('{"dataDir": "data"}', encoding="utf-8")
    try:
        config.override_watch_interval(45)
        assert config.load_config(str(path))["watch"]["intervalSeconds"] == 45
    finally:
        config.override_watch_interval(None)
    assert "watch" not in config.load_config(str(path))
