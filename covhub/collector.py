"""push 通道收集端。"""

from datetime import datetime
import os
import re
import socket
import threading
import time

from .config import find_service
from .exec_format import remote_dump, write_exec_file
from .layout import ensure_dirs
from .locks import LOCK
from .logbuf import log

# --------------------------------------------------------------------------
# push 通道：agent 主动连上来（output=tcpclient）
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


def _class_ids_in(execdata):
    """一次 dump 里出现过的 class id 集合。id 就是 JaCoCo 的 CRC64 指纹。"""
    return frozenset(cid for cid, _, _ in execdata)
def _sessionid_to_service(cfg, sessionid):
    """sessionid 形如 <服务名> 或 <服务名>#<任意后缀>，取前段去匹配服务。"""
    head = re.split(r"[#@]", sessionid or "", 1)[0]
    for svc in cfg.get("services", []):
        if svc["name"] in (sessionid, head):
            return svc["name"]
    return None
class PushCollector:
    """接收 tcpclient agent 的长连接，并在需要时向它们要数据。"""

    def __init__(self, cfg_loader):
        # 注入「怎么取当前配置」而不是配置本身：认领连接时要看最新的服务列表
        # （配置每次重读是硬约束），而收集端不该知道配置来自文件还是数据库。
        self.cfg_loader = cfg_loader
        self.conns = {}
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
        log("push 收集端已监听 %s:%d（等待 output=tcpclient 的 agent 连入）" % (bind, port))

    def _accept_loop(self):
        """accept 循环。除了监听 socket 真的关了，任何错误都不许让它退出。

        原先这里 `except OSError: return` —— fd 临时耗尽、对端在 accept 前就
        重置连接这类瞬时错误，会让收集端从此**永久不再接客**，而且一声不吭。
        push 通道无声停摆和守护进程无声停摆是一回事。
        """
        while True:
            try:
                sock, peer = self.srv.accept()
            except OSError as exc:
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
                    "classIds": _class_ids_in(execdata),
                }

            if not service:
                log('push：%s 连上来了，但 sessionid "%s" 匹配不到任何服务 —— '
                    "配置里的服务名和 agent 的 sessionid 要对上" % (who, sessionid))
                return

            log('push：%s 连入，认领为服务 %s（sessionid "%s"）' % (who, service, sessionid))
            # 握手那一次拿到的数据本身就是有效数据，直接存下，别浪费
            svc = find_service(cfg, service)
            with LOCK:
                self._store(cfg, svc, cid, sessions, execdata)
        except (Exception, SystemExit) as exc:
            why = str(exc) or type(exc).__name__
            log("push：来自 %s 的连接没能接住 —— %s" % (who, why))
            if cid is None:
                _close_quietly(sock)
            else:
                self.drop(cid, "认领失败")

    # ---- 数据 ----

    def _store(self, cfg, svc, cid, sessions, execdata):
        root = ensure_dirs(cfg, svc)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(root, "exec", "%s-push%d.exec" % (ts, cid))
        write_exec_file(path, sessions, execdata)
        with self.lock:
            if cid in self.conns:
                self.conns[cid]["last"] = datetime.now().isoformat(timespec="seconds")
                # 这一版跑的是哪份 class，跟着每次取数刷新（见 mixed_versions）
                self.conns[cid]["classIds"] = _class_ids_in(execdata)
        return path

    def mixed_versions(self, service):
        """在线实例里是不是同时跑着两份不同的 class。

        这是 push 通道下 pull 那套「断代检测」的对应物。pull 是单实例，进程一重启
        计数器就归零，混桶必然出错，所以必须封存；push 是多副本，副本重启后数据
        照样能 merge —— 只要跑的是**同一份 class**。真正会让报告出错的是滚动发版
        中途：新旧副本的数据落进同一批 exec，对着任何一份 class 产物都只能对上一半。

        判断只看 class id 集合的包含关系：同一份产物、加载进度不同 → 互为子集；
        真的换了版本 → 双方都有对方没有的 id。这条不依赖任何人填的版本号。
        """
        sets = [c["classIds"] for c in self.instances(service) if c.get("classIds")]
        for i in range(len(sets)):
            for j in range(i + 1, len(sets)):
                if (sets[i] - sets[j]) and (sets[j] - sets[i]):
                    return True
        return False

    def instances(self, service):
        with self.lock:
            return [c for c in self.conns.values() if c["service"] == service]

    def drop(self, cid, why):
        with self.lock:
            conn = self.conns.pop(cid, None)
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

        workers = [threading.Thread(target=fetch, args=(c,), daemon=True)
                   for c in conns]
        for w in workers:
            w.start()
        for w in workers:
            w.join()

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
    return _COLLECTOR.instances(service) if _COLLECTOR else []
