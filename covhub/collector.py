"""push 通道收集端。"""

from datetime import datetime
import os
import re
import socket
import contextvars
import threading
import time

from .config import find_service
from .exec_format import remote_dump, write_exec_file
from .layout import ensure_dirs
from .locks import LOCK
from .logbuf import log

# --------------------------------------------------------------------------
# push 通道：agent 主动连上来（covhub-agent.jar，或 JaCoCo 自带的 output=tcpclient ——
# 两者在线上说的是同一套协议，收集端分不出也不需要分）
#
# 适用于 pull 够不着的场景：被测端不能开入站端口、容器网络只出不进、多副本还会
# 自动扩缩 —— 后者用 pull 得给每个副本配一条，用 push 则是副本自己连过来，
# 数据天然汇到同一个服务桶里。
#
# 有两点和 pull 不一样，写在这儿免得后来人踩：
#
#   1. **agent 不会主动报自己是谁。** 连上来只有一个 TCP 连接，要等 dump 回来的
#      SessionInfo 才知道 sessionid。所以 accept 之后立刻 dump 一次做认领，
#      那一次的数据本身也是有效数据，直接存下。
#
#   2. **收集端必须和采集在同一个进程里。** 连接是长连接、握在收集端手上，
#      另起一个 watch 进程够不着它。所以 push 通道要求 serve --with-watch。
# --------------------------------------------------------------------------

# 认领新连接时等第一次 dump 的上限 —— agent 刚连上、类还没加载完也走这条路。
HANDSHAKE_TIMEOUT = 30
# 之后每次取数的上限，可用 collect.dumpTimeoutSeconds 调。这是**单次 recv** 的
# 上限，不是总时长：正常 dump 每个分片都有数据，只有对端真卡住才会等满。
DEFAULT_DUMP_TIMEOUT = 20
def _close_quietly(*targets):
    """关连接时的错误一律不关心 —— 走到这儿说明它已经没用了。"""
    for target in targets:
        try:
            target.close()
        except Exception:
            pass


def _classes_in(execdata):
    """一次 dump 里出现过的类：{类名: class id}。id 就是 JaCoCo 的 CRC64 指纹。

    注意这是「到此为止**执行过**的类」，不是已加载的类 —— JaCoCo 的 ExecutionDataWriter
    只写 hasHits() 的条目。所以两份数据不能拿 id 集合比包含关系，得按类名对 id。
    """
    return {name: cid for cid, name, _ in execdata}
def _sessionid_to_service(cfg, sessionid):
    """sessionid 形如 <服务名> 或 <服务名>#<任意后缀>，取前段去匹配服务。"""
    head = re.split(r"[#@]", sessionid or "", 1)[0]
    for svc in cfg.get("services", []):
        if svc["name"] in (sessionid, head):
            return svc["name"]
    return None
class PushCollector:
    """接收被测端 agent 的长连接，并在需要时向它们要数据。"""

    def __init__(self, cfg_loader):
        # 注入「怎么取当前配置」而不是配置本身：认领连接时要看最新的服务列表
        # （配置每次重读是硬约束），而收集端不该知道配置来自文件还是数据库。
        self.cfg_loader = cfg_loader
        self.conns = {}
        # 按服务记的两组「JVM 会话起点」（SessionInfo 的 start，毫秒；它就是一个 JVM 的身份，
        # 重启才会变，--reset 也会把它推到清零那一刻）：
        #   dropped      掉线后还没回来的实例
        #   missed_reset 结算清零时不在线（或取数失败被丢弃）的实例 —— 它们的计数器还是上一
        #                周期的，重连时握手带回来的数据属于已归档的旧版本，不能混进新周期
        self.dropped = {}
        self.missed_reset = {}
        self.lock = threading.Lock()
        self.seq = 0
        self.srv = None

    # ---- 生命周期 ----

    def start(self, port, bind="0.0.0.0"):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((bind, port))
        self.srv.listen(64)
        threading.Thread(target=self._accept_loop, daemon=True).start()
        log("push 收集端已监听 %s:%d（等待被测端的 agent 连入）" % (bind, port))

    def _accept_loop(self):
        """accept 循环。除了监听 socket 真的关了，任何错误都不许让它退出。

        原先这里 `except OSError: return` —— fd 临时耗尽、对端在 accept 前就
        重置连接这类瞬时错误，会让收集端从此**永久不再接客**，而且一声不吭。
        push 通道无声停摆和守护进程无声停摆是一回事。
        """
        while True:
            try:
                sock, peer = self.srv.accept()
            except (OSError, AttributeError) as exc:
                if self.srv is None or self.srv.fileno() < 0:
                    return                      # 监听 socket 已关闭，正常收场
                log("! push 收集端 accept 出错，1 秒后重试 —— %s" % exc)
                time.sleep(1)
                continue
            try:
                threading.Thread(target=self._claim, args=(sock, peer),
                                 daemon=True).start()
            except RuntimeError as exc:
                # 起不了线程也不能拖垮循环，回绝这一条就是了
                log("! push：起不了处理线程，回绝 %s:%d —— %s" % (peer[0], peer[1], exc))
                _close_quietly(sock)

    def _claim(self, sock, peer):
        """认领一条新连接：立刻 dump 一次，从 SessionInfo 里读出它是谁。

        整段包在一个 except 里，而且**连 SystemExit 一起接**做保险：这个线程
        要是静默死亡，就是连接泄漏，既没有日志也没有回收。
        """
        who = "%s:%d" % peer
        cid = None
        try:
            sock.settimeout(HANDSHAKE_TIMEOUT)
            rfile, wfile = sock.makefile("rb"), sock.makefile("wb")
            sessions, execdata = remote_dump(rfile, wfile, reset=False)

            sessionid = sessions[0][0] if sessions else ""
            cfg = self.cfg_loader()
            service = _sessionid_to_service(cfg, sessionid)
            with self.lock:
                self.seq += 1
                cid = self.seq
                self.conns[cid] = {
                    "id": cid, "peer": who, "sessionid": sessionid, "service": service,
                    "since": datetime.now().isoformat(timespec="seconds"),
                    "last": None, "sock": sock, "rfile": rfile, "wfile": wfile,
                    "sessionStart": sessions[0][1] if sessions else None,
                    "classes": _classes_in(execdata),
                }

            if not service:
                log('push：%s 连上来了，但 sessionid "%s" 匹配不到任何服务 —— '
                    "配置里的服务名和 agent 的 sessionid 要对上" % (who, sessionid))
                self._park(cid, sock, rfile)
                return

            log('push：%s 连入，认领为服务 %s（sessionid "%s"）' % (who, service, sessionid))
            svc = find_service(cfg, service)
            start = sessions[0][1] if sessions else None
            self._retire_twin(cid, service, peer[0], start)
            with self.lock:
                self.dropped.get(service, set()).discard(start)
                stale = start is not None and start in self.missed_reset.get(service, set())
                if stale:
                    self.missed_reset[service].discard(start)
            if stale:
                # 这个 JVM 错过了结算时的清零：握手拿到的是上一周期（已归档）的累计数据，
                # 写进新周期只会把新版本的数字抬高、指纹匹配率拉低。清零，从头累加
                log("push：%s 错过了 %s 上次结算的清零，握手带回的是上一周期的数据 —— 丢弃并清零"
                    % (who, service))
                remote_dump(rfile, wfile, reset=True)
                with self.lock:
                    if cid in self.conns:
                        self.conns[cid]["sessionStart"] = None      # 下次取数时读到新的
                        self.conns[cid]["classes"] = {}
                return
            with LOCK:
                with self.lock:
                    dumped = cid in self.conns and self.conns[cid]["last"] is not None
                if dumped:
                    # 登记和拿到 LOCK 之间采集线程已经取过这条连接（可能还带 --reset）：
                    # 握手那份比它旧，带 reset 时更是上一周期的，不能再写
                    return
                # 握手那一次拿到的数据本身就是有效数据，直接存下，别浪费
                try:
                    self._store(cfg, svc, cid, sessions, execdata)
                except Exception as exc:
                    # 取到了却写不下去是 hub 这边的问题（磁盘满、权限），别把实例当断线踢掉：
                    # covhub-agent 连上就把退避重置回 1 秒，踢掉等于让它 1 秒一次地重连并全量 dump
                    log("push：%s 的握手数据落盘失败 —— %s" % (who, exc))
        except (Exception, SystemExit) as exc:
            why = str(exc) or type(exc).__name__
            log("push：来自 %s 的连接没能接住 —— %s" % (who, why))
            if cid is None:
                _close_quietly(sock)
            else:
                self.drop(cid, "认领失败")

    def _park(self, cid, sock, rfile):
        """sessionid 匹配不到服务的连接：不取数，但也不能登记完就撒手。

        撒手的后果是泄漏：这边没人读它，agent 到 idle 自己断开重连，每次重连都多一条
        永远不释放的记录和 fd —— 2.6 起 covhub-agent 会一直重连，泄漏没有上限，fd 耗尽后
        正常实例也连不进来。直接关掉也不好：covhub-agent 连上就把退避重置回 1 秒，会
        1 秒一次地握手、全量 dump、刷被测端的日志。所以阻塞读到对方断开再释放（agent 不会
        主动发任何东西，读到的只会是 EOF）；登记上服务之后它下一次重连（最多 idle 秒）就会
        被认领。
        """
        try:
            sock.settimeout(None)
            while rfile.read(1):
                pass
        except Exception:
            pass
        self.drop(cid, "sessionid 匹配不到服务，等它重连后再认领")

    # ---- 数据 ----

    def _store(self, cfg, svc, cid, sessions, execdata):
        root = ensure_dirs(cfg, svc)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(root, "exec", "%s-push%d.exec" % (ts, cid))
        n = 1
        while os.path.exists(path):
            # 握手落盘紧跟在一轮取数之后时会同秒同 cid：覆盖掉的是更新的那份
            n += 1
            path = os.path.join(root, "exec", "%s-push%d-%d.exec" % (ts, cid, n))
        write_exec_file(path, sessions, execdata)
        with self.lock:
            if cid in self.conns:
                self.conns[cid]["last"] = datetime.now().isoformat(timespec="seconds")
                # 这一版跑的是哪份 class，跟着每次取数刷新（见 mixed_versions）
                self.conns[cid]["classes"] = _classes_in(execdata)
                if sessions:
                    # --reset 会把会话起点推到清零那一刻，之后它就是这个 JVM 的新身份
                    self.conns[cid]["sessionStart"] = sessions[0][1]
        return path

    def mixed_versions(self, service):
        """在线实例里是不是同时跑着两份不同的 class。

        这是 push 通道下 pull 那套「断代检测」的对应物。pull 是单实例，进程一重启
        计数器就归零，混桶必然出错，所以必须封存；push 是多副本，副本重启后数据
        照样能 merge —— 只要跑的是**同一份 class**。真正会让报告出错的是滚动发版
        中途：新旧副本的数据落进同一批 exec，对着任何一份 class 产物都只能对上一半。

        判断按类名对 id：两边都执行过的同名类 id 不同 → 换了版本。**不能**比 id 集合的
        包含关系：exec 里只有执行过的类（JaCoCo 只写 hasHits() 的条目），两个副本各跑各的
        请求路径，集合天然互有对方没有的 id，按包含关系判会把正常的负载均衡当成混版本
        （predeploy --reset 之后集合从零长起，更容易分叉）。这条仍不依赖任何人填的版本号。
        """
        seen = [c["classes"] for c in self.instances(service) if c.get("classes")]
        for i in range(len(seen)):
            for j in range(i + 1, len(seen)):
                a, b = seen[i], seen[j]
                if any(a[name] != b[name] for name in a.keys() & b.keys()):
                    return True
        return False

    def stop(self):
        """进程退出时收尾：关监听、断掉所有实例。

        实例那头挂的是 covhub-agent 才会自己重连；JaCoCo 自带的 tcpclient 断了就不再连，
        那些实例要等被测服务重启才回来 —— 这正是 covhub-agent 存在的原因之一。
        """
        srv, self.srv = self.srv, None
        _close_quietly(srv)
        with self.lock:
            cids = list(self.conns)
        for cid in cids:
            self.drop(cid, "hub 退出")

    def instances(self, service):
        with self.lock:
            return [c for c in self.conns.values() if c["service"] == service]

    def _retire_twin(self, cid, service, host, start):
        """同一个 JVM 重连上来（idle 到点、网络抖动）：它的旧连接已经死了，但这边没读过
        所以不知道。不清掉的话，到下一次取数把它丢弃之前，看板和 instances 都会把一个 JVM
        数成两个，混版本记录里的实例数也虚高。同服务、同主机、同会话起点 = 同一个 JVM。"""
        if start is None:
            return
        with self.lock:
            twins = [c["id"] for c in self.conns.values()
                     if c["id"] != cid and c["service"] == service
                     and c.get("sessionStart") == start and c["peer"].rsplit(":", 1)[0] == host]
        for old in twins:
            self.drop(old, "同一个 JVM 已重新连入", forget=False)

    def drop(self, cid, why, forget=True):
        with self.lock:
            conn = self.conns.pop(cid, None)
            if conn and forget and conn.get("service") and conn.get("sessionStart") is not None:
                # 记住它走了：下次结算时它若还没回来，就是错过了清零（见 dump_service）
                self.dropped.setdefault(conn["service"], set()).add(conn["sessionStart"])
        if conn:
            log("push：实例 %s 已断开（%s）" % (conn["peer"], why))
            for key in ("rfile", "wfile", "sock"):
                try:
                    conn[key].close()
                except Exception:
                    pass

    def dump_service(self, cfg, svc, reset=False):
        """向该服务当前所有在线实例各要一次数据，返回落盘的文件数。

        取数并行，落盘串行。两件事都是有意的：

          · **并行** —— 实例就是多副本，串行的话总耗时是各实例之和。一个网络
            分区的实例（TCP 收不到 FIN）要等满 socket 超时才报错，而调用方是
            握着 LOCK 进来的 —— 串行等于让 N 个坏实例把整个 hub 冻住 N 倍的
            超时。并行之后最坏等待不再随副本数放大。
          · **落盘串行** —— write_exec_file 写的是同一个 exec 目录，交给调用方
            那把锁保护，别在工作线程里各写各的。
        """
        conns = self.instances(svc["name"])
        if not conns:
            return 0

        timeout = (cfg.get("collect") or {}).get("dumpTimeoutSeconds",
                                                 DEFAULT_DUMP_TIMEOUT)
        got = []

        def fetch(conn):
            try:
                conn["sock"].settimeout(timeout)
                sessions, execdata = remote_dump(conn["rfile"], conn["wfile"],
                                                 reset=reset)
                got.append((conn, sessions, execdata))     # list.append 本身是原子的
            except (Exception, SystemExit) as exc:
                # 实例没了。它最后一段数据随进程消失 —— 和 pull 一样没有补救手段
                self.drop(conn["id"], str(exc))

        # copy_context：fetch 里 drop() 打的「实例已断开」要进调用方（HTTP 请求）的日志汇，
        # 否则 predeploy 掉了一个副本，返回体里一字不提
        workers = [threading.Thread(target=contextvars.copy_context().run, args=(fetch, c),
                                    daemon=True)
                   for c in conns]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        if reset:
            # 清零这一刻没应答的（取数失败已被丢弃）和之前掉线还没回来的，计数器都还是
            # 上一周期的：记下来，等它们重连时把握手数据丢掉并清零（见 _claim）
            answered = {conn["id"] for conn, _, _ in got}
            with self.lock:
                missed = self.missed_reset.setdefault(svc["name"], set())
                missed.update(c["sessionStart"] for c in conns
                              if c["id"] not in answered and c.get("sessionStart") is not None)
                missed.update(self.dropped.pop(svc["name"], set()))

        written = 0
        for conn, sessions, execdata in got:
            try:
                self._store(cfg, svc, conn["id"], sessions, execdata)
                written += 1
            except Exception as exc:
                # 取到了却写不下去，是 hub 这边的问题，别把实例当断线丢掉
                log("push：%s 的数据落盘失败 —— %s" % (conn["peer"], exc))
        return written


# 进程内唯一的收集端。只在 serve 起了收集端时非 None；别的进程（比如直接跑 CLI）
# 看不到它手上的连接 —— 那是「不知道」不是「离线」，上层要如实区分。
_COLLECTOR = None


def set_collector(collector):
    global _COLLECTOR
    _COLLECTOR = collector


def get_collector():
    return _COLLECTOR


def collector_instances(service):
    """给收集端之外的调用方（看板、status、断代检测）看的在线实例列表。

    只给能直接进 JSON 的几个字段。conns 里的原始记录握着 socket / 文件对象和
    classIds 集合 —— 2.6 之前曾把它原样塞进详情接口的返回体，push 服务一有在线实例
    详情页就 500（socket 不能序列化）。sock 之类只有收集端自己该碰。
    """
    if not _COLLECTOR:
        return []
    return [{"id": c["id"], "peer": c["peer"], "sessionid": c["sessionid"],
             "since": c["since"], "last": c["last"], "sessionStart": c.get("sessionStart")}
            for c in _COLLECTOR.instances(service)]
