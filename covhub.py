#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""covhub —— 通用 JaCoCo 运行期覆盖率采集与看板。

与被测服务无关：只要目标 JVM 挂了 output=tcpserver 模式的 jacocoagent，
就能被本工具采集、归档、渲染成可在线查看的报告。

子命令：
    agent-opts <service>        打印该服务启动时应注入的 -javaagent 参数串
    status [service]            列出目标连通性与最新覆盖率
    dump <service>              拉一次快照并生成报告（累加，不清零）
    predeploy <service>         发版/重启前结算：dump --reset + 归档 + 出终版报告
    watch                       守护进程：按间隔轮询全部目标
    serve                       起 HTTP 服务：看板 + 远程控制 API（可同时跑采集）
    report <service>            从已有 exec 重新生成报告
    retarget <service>          发版后更新配置里的 version / classfiles
    diagnose <service>          诊断 exec 与 class 产物是否对得上

整套方案只需要**一个** covhub 服务端。被测服务所在的机器、发版节点都不需要装
Python 或 java —— 它们通过 serve 暴露的 HTTP API 驱动 hub 干活（见 --help 或
integration/covhub-client.sh）。

配置文件用 YAML 或 JSON 都行，按扩展名分派；缺省在当前目录按
targets.yaml / targets.yml / targets.json 顺序探测，可用 -c 指定。
YAML 需要 PyYAML，这是唯一的第三方依赖 —— 用 JSON 则完全零依赖。
"""

import argparse
import contextlib
import csv
import hashlib
import http.server
import io
import json
import os
import re
import secrets
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import zipfile
from datetime import datetime
from html import escape as html_escape

__version__ = "1.3.0"

COUNTERS = ["INSTRUCTION", "BRANCH", "LINE", "COMPLEXITY", "METHOD"]


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

CONFIG_CANDIDATES = ("targets.yaml", "targets.yml", "targets.json")


def config_format(path):
    """按扩展名判断配置格式，.yaml / .yml 走 YAML，其余按 JSON。"""
    return "yaml" if os.path.splitext(path)[1].lower() in (".yaml", ".yml") else "json"


def resolve_config_path(explicit):
    """-c 没给时按 targets.yaml → targets.yml → targets.json 顺序探测。

    两种格式长期并存：已有部署的 targets.json 原样能跑，新机器默认用 YAML
    （能写注释、不用数逗号）。都不存在时返回推荐的那个，让报错指向 YAML。
    """
    if explicit:
        return explicit
    for name in CONFIG_CANDIDATES:
        if os.path.isfile(name):
            return name
    return CONFIG_CANDIDATES[0]


def read_config_file(path):
    """读配置原文并解析成 dict，不做路径规整（retarget 也用它做匹配）。"""
    if not os.path.isfile(path):
        die("找不到配置文件 %s，先运行 covhub.py init 生成模板" % path)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if config_format(path) == "yaml":
        data = parse_yaml(text, path)
    else:
        try:
            data = json.loads(text)
        except ValueError as exc:
            die("配置文件 %s 不是合法 JSON：%s" % (path, exc))
    if data is None:
        data = {}
    if not isinstance(data, dict):
        die("配置文件 %s 的顶层必须是对象" % path)
    return data


def parse_yaml(text, path):
    """YAML 解析依赖 PyYAML。

    工具其余部分只用标准库，YAML 是唯一的例外 —— 装不了第三方包的机器
    （离线内网、老镜像）可以继续用 JSON，两种格式功能完全等价。
    """
    try:
        import yaml
    except ImportError:
        die("解析 %s 需要 PyYAML：pip install PyYAML\n"
            "        装不上的话可以用 JSON 配置：covhub.py init --json" % path)
    try:
        return yaml.safe_load(text)
    except Exception as exc:
        die("配置文件 %s 解析失败：%s" % (path, exc))


def load_config(path):
    cfg = read_config_file(path)
    base = os.path.dirname(os.path.abspath(path))
    # 相对路径一律相对配置文件所在目录解析，便于整个目录搬迁
    for key in ("jacocoCli", "jacocoAgent", "dataDir"):
        if cfg.get(key) and not os.path.isabs(cfg[key]):
            cfg[key] = os.path.normpath(os.path.join(base, cfg[key]))
    for svc in cfg.get("services", []):
        for key in ("classfiles", "sourcefiles"):
            svc[key] = [p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))
                        for p in svc.get(key, [])]
    return cfg


def find_service(cfg, name):
    for svc in cfg.get("services", []):
        if svc["name"] == name:
            return svc
    die("配置里没有名为 %r 的服务，已定义：%s"
        % (name, ", ".join(s["name"] for s in cfg.get("services", []))))


def die(msg, code=1):
    print("[covhub] 错误：" + msg, file=sys.stderr)
    sys.exit(code)


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


# --------------------------------------------------------------------------
# 目录布局
# --------------------------------------------------------------------------
#   <dataDir>/
#     index.html                看板首页（watch / dump 后自动刷新）
#     <service>/
#       current/                最新报告，看板直接指向这里
#       exec/<ts>.exec          历次快照原始数据
#       versions/<version>/     发版结算归档（报告 + exec + manifest）
#       artifacts/<version>/    经 upload-classes 传上来的 class 产物
#       classes/                按 reportExcludes 过滤后的 class 副本
#       state.json              历史统计，用于趋势
# --------------------------------------------------------------------------

def svc_dir(cfg, svc):
    return os.path.join(cfg["dataDir"], svc["name"])


def ensure_dirs(cfg, svc):
    root = svc_dir(cfg, svc)
    for sub in ("current", "exec", "versions", "classes", "artifacts"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    return root


# --------------------------------------------------------------------------
# class 过滤：CLI 的 report 命令不支持 excludes，只能先过滤出一份副本
# --------------------------------------------------------------------------

def ant_to_regex(pattern):
    """把 Ant 风格路径模式编译成正则。** 跨目录，* 不跨目录。"""
    out, i = "", 0
    while i < len(pattern):
        if pattern[i:i + 3] == "**/":
            out += "(?:.*/)?"
            i += 3
        elif pattern[i:i + 2] == "**":
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile("^" + out + "$")


def prepare_classfiles(cfg, svc):
    """按 reportExcludes 过滤 class，返回可直接喂给 --classfiles 的路径列表。

    没有配置 reportExcludes 时原样返回，不做任何拷贝。
    """
    patterns = svc.get("reportExcludes") or []
    if not patterns:
        return svc["classfiles"]

    regexes = [ant_to_regex(p) for p in patterns]
    dest_root = os.path.join(svc_dir(cfg, svc), "classes")
    shutil.rmtree(dest_root, ignore_errors=True)

    kept = dropped = 0
    results = []
    for idx, src in enumerate(svc["classfiles"]):
        if not os.path.isdir(src):
            # jar 文件无法逐类过滤，原样透传并提示
            log("  ! %s 不是目录，reportExcludes 对它不生效" % src)
            results.append(src)
            continue
        dest = os.path.join(dest_root, "cp%d" % idx)
        for dirpath, _, files in os.walk(src):
            for name in files:
                if not name.endswith(".class"):
                    continue
                full = os.path.join(dirpath, name)
                vm_name = os.path.relpath(full, src).replace("\\", "/")[:-len(".class")]
                if any(r.match(vm_name) for r in regexes):
                    dropped += 1
                    continue
                target = os.path.join(dest, os.path.relpath(full, src))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copyfile(full, target)
                kept += 1
        results.append(dest)
    log("  class 过滤：保留 %d，按 reportExcludes 剔除 %d" % (kept, dropped))
    return results


# --------------------------------------------------------------------------
# 与 agent / CLI 交互
# --------------------------------------------------------------------------

def agent_opts(cfg, svc):
    """生成 -javaagent 参数串。通用做法：塞进 JAVA_TOOL_OPTIONS 环境变量。"""
    if service_channel(svc) == "push":
        # agent 主动连回 hub。address 必须是**被测端能访问到的** hub 地址，
        # 不是 hub 自己的监听地址 —— 跨网段、容器里最容易在这儿配错。
        collect = cfg.get("collect") or {}
        addr = collect.get("advertiseAddress")
        if not addr:
            die("服务 %s 用的是 push 通道，需要配置 collect.advertiseAddress"
                "（被测端连回 hub 用的地址）" % svc["name"])
        opts = [
            "output=tcpclient",
            "address=%s" % addr,
            "port=%d" % collect.get("port", 6400),
        ]
    else:
        opts = [
            "output=tcpserver",
            "address=%s" % svc.get("bindAddress", "0.0.0.0"),
            "port=%d" % svc["port"],
        ]
    if svc.get("includes"):
        opts.append("includes=" + ":".join(svc["includes"]))
    if svc.get("excludes"):
        opts.append("excludes=" + ":".join(svc["excludes"]))
    if svc.get("classDumpDir"):
        # 让 agent 把它实际加载到的 class 落盘。这份 class 与 exec 的 class id
        # 不是「应该匹配」，是定义上必然匹配 —— 出报告时用它，不会再有全红。
        opts.append("classdumpdir=%s" % svc["classDumpDir"])
    # push 通道靠 sessionid 认领连接，必须是服务名；pull 通道沿用版本号做标记
    opts.append("sessionid=%s" % (svc["name"] if service_channel(svc) == "push"
                                  else svc.get("version", svc["name"])))
    return "-javaagent:%s=%s" % (cfg["jacocoAgent"], ",".join(opts))


def reachable(svc, timeout=2.0):
    if service_channel(svc) == "push":
        # push 通道没有可探的端口，「在线」等于当前有实例连着
        return bool(collector_instances(svc["name"]))
    try:
        with socket.create_connection((svc["address"], svc["port"]), timeout):
            return True
    except OSError:
        return False


def run_cli(cfg, args, quiet=True):
    cmd = ["java", "-jar", cfg["jacocoCli"]] + args
    if quiet:
        cmd.append("--quiet")
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip()[:600])
    return proc.stdout


# --------------------------------------------------------------------------
# JaCoCo exec 二进制格式与 remote control 协议
#
# 为什么要自己实现：jacococli 只有 dump（去连 output=tcpserver 的 agent），
# 没有「收集端」命令。要接 output=tcpclient —— agent 主动连过来的那种 —— 方向
# 反了，官方工具帮不上忙，这段协议只能自己写。
#
# 格式定义在 org.jacoco.core.data.ExecutionDataWriter / CompactDataOutput，
# 下面的常量是从真实 exec 文件头实测出来的，不是照抄文档：
#     01 c0 c0 10 07 | 10 00 0b 63 6f 76 ...
#     ^块类型 ^magic ^版本 | ^SESSIONINFO ^UTF长度 ^id
#
# 数值一律大端；字符串是 Java 的 modified UTF-8（2 字节长度 + 内容）；
# 布尔数组是 varint 长度 + 位压缩，每字节低位在前。
# --------------------------------------------------------------------------

EXEC_MAGIC = 0xC0C0
EXEC_VERSION = 0x1007
BLOCK_HEADER = 0x01
BLOCK_SESSIONINFO = 0x10
BLOCK_EXECUTIONDATA = 0x11
BLOCK_CMDDUMP = 0x40
BLOCK_CMDOK = 0x20


def _enc_varint(value):
    out = bytearray()
    while True:
        if value & ~0x7F:
            out.append(0x80 | (value & 0x7F))
            value >>= 7
        else:
            out.append(value)
            return bytes(out)


def _enc_utf(text):
    raw = text.encode("utf-8")
    if len(raw) > 0xFFFF:
        raise RuntimeError("字符串过长，超出 JaCoCo 的 UTF 长度上限")
    return struct.pack(">H", len(raw)) + raw


def _enc_bools(bits):
    out = bytearray(_enc_varint(len(bits)))
    buf = size = 0
    for b in bits:
        if b:
            buf |= 1 << size
        size += 1
        if size == 8:
            out.append(buf)
            buf = size = 0
    if size:
        out.append(buf)
    return bytes(out)


class ExecReader:
    """从一个 file-like（socket.makefile('rb') 或普通文件）按 JaCoCo 格式读记录。"""

    def __init__(self, fp):
        self.fp = fp

    def raw(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.fp.read(n - len(buf))
            if not chunk:
                raise EOFError("连接在读取中断开")
            buf += chunk
        return buf

    def u8(self):
        return self.raw(1)[0]

    def u16(self):
        return struct.unpack(">H", self.raw(2))[0]

    def i64(self):
        return struct.unpack(">q", self.raw(8))[0]

    def boolean(self):
        return self.u8() != 0

    def varint(self):
        value = shift = 0
        while True:
            b = self.u8()
            value |= (b & 0x7F) << shift
            if not b & 0x80:
                return value
            shift += 7

    def utf(self):
        return self.raw(self.u16()).decode("utf-8", "replace")

    def bools(self):
        count = self.varint()
        bits = []
        buf = 0
        for i in range(count):
            if i % 8 == 0:
                buf = self.u8()
            bits.append(bool(buf & (1 << (i % 8))))
        return bits


def exec_header():
    return struct.pack(">BHH", BLOCK_HEADER, EXEC_MAGIC, EXEC_VERSION)


def write_exec_file(path, sessions, execdata):
    """把收上来的记录写成 jacococli 能直接读的 .exec。"""
    with open(path, "wb") as f:
        f.write(exec_header())
        for sid, start, dump in sessions:
            f.write(struct.pack(">B", BLOCK_SESSIONINFO) + _enc_utf(sid)
                    + struct.pack(">qq", start, dump))
        for cid, name, probes in execdata:
            f.write(struct.pack(">Bq", BLOCK_EXECUTIONDATA, cid) + _enc_utf(name)
                    + _enc_bools(probes))
    return path


def remote_dump(rfile, wfile, reset=False):
    """在一条已建立的连接上发 dump 命令并收数据，返回 (sessions, execdata)。

    双方在连接建立后各自先发一个 header —— agent 的 reader 上来就要读它，
    不先发过去，对方会一直卡在那儿。
    """
    wfile.write(exec_header())
    wfile.write(struct.pack(">B??", BLOCK_CMDDUMP, True, bool(reset)))
    wfile.flush()

    reader = ExecReader(rfile)
    sessions, execdata = [], []
    while True:
        block = reader.u8()
        if block == BLOCK_HEADER:
            magic, version = reader.u16(), reader.u16()
            if magic != EXEC_MAGIC:
                raise RuntimeError("对端不是 JaCoCo agent（magic 0x%04x）" % magic)
            if version != EXEC_VERSION:
                # 只警告不拒绝：格式版本变了通常仍能读，读错了下面自然会炸
                log("  ! agent 的 exec 格式版本是 0x%04x，本工具按 0x%04x 解析"
                    % (version, EXEC_VERSION))
        elif block == BLOCK_SESSIONINFO:
            sessions.append((reader.utf(), reader.i64(), reader.i64()))
        elif block == BLOCK_EXECUTIONDATA:
            execdata.append((reader.i64(), reader.utf(), reader.bools()))
        elif block == BLOCK_CMDOK:
            return sessions, execdata
        else:
            raise RuntimeError("协议里出现未知块类型 0x%02x" % block)


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


def service_channel(svc):
    return (svc.get("channel") or "pull").lower()


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


def endpoint_label(svc):
    """一句话描述这个服务在哪儿取数。push 服务没有 address/port，别直接摸那两个字段。"""
    if service_channel(svc) == "push":
        return "push · %d 个实例" % len(collector_instances(svc["name"]))
    return "%s:%d" % (svc["address"], svc["port"])


def _sessionid_to_service(cfg, sessionid):
    """sessionid 形如 <服务名> 或 <服务名>#<任意后缀>，取前段去匹配服务。"""
    head = re.split(r"[#@]", sessionid or "", 1)[0]
    for svc in cfg.get("services", []):
        if svc["name"] in (sessionid, head):
            return svc["name"]
    return None


class PushCollector:
    """接收 tcpclient agent 的长连接，并在需要时向它们要数据。"""

    def __init__(self, cfg_path):
        self.cfg_path = cfg_path
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

        整段包在一个 except 里，而且**连 SystemExit 一起接**。这里会调
        load_config / find_service，它们内部走的是 die()，抛的是 SystemExit ——
        它不是 Exception 的子类，漏出去就是这个线程静默死亡加连接泄漏，
        既没有日志也没有回收。
        """
        who = "%s:%d" % peer
        cid = None
        try:
            sock.settimeout(HANDSHAKE_TIMEOUT)
            rfile, wfile = sock.makefile("rb"), sock.makefile("wb")
            sessions, execdata = remote_dump(rfile, wfile, reset=False)

            sessionid = sessions[0][0] if sessions else ""
            cfg = load_config(self.cfg_path)
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
            with _LOCK:
                self._store(cfg, svc, cid, sessions, execdata)
        except (Exception, SystemExit) as exc:
            # SystemExit 的 str() 只有退出码，说明是 die() 刚打到 stderr 的那条
            why = "配置读取或服务查找失败（详见上一行）" if isinstance(exc, SystemExit)                 else str(exc)
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
            握着 _LOCK 进来的 —— 串行等于让 N 个坏实例把整个 hub 冻住 N 倍的
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


_COLLECTOR = None


def collector_instances(service):
    return _COLLECTOR.instances(service) if _COLLECTOR else []


# --------------------------------------------------------------------------
# jacococli 输出解析
#
# classinfo / execinfo 这两个命令此前一次都没用上，而 JaCoCo 把判断「数据还
# 有没有效」所需要的东西全放在它们的输出里了：
#
#   classinfo : "  <计数列>   class 0x<16位指纹> <类名>"
#   execinfo  : 'Session "<id>": <启动时刻> - <dump 时刻>'
#               "<16位指纹>  <命中> of <探针>   <类名>"
#
# 会话的**启动时刻**是判断被测进程有没有重启过的唯一凭据，且不需要任何人配合。
# 它是 Java Date.toString() 的输出，带时区和 locale —— 不去解析它，只比对字符串
# 是否变化，这样既准确又不受环境影响。
# --------------------------------------------------------------------------

RE_CLASSINFO = re.compile(r"class 0x([0-9a-f]{16})\s+(\S+)")
RE_EXEC_CLASS = re.compile(r"^([0-9a-f]{16})\s+(\d+) of\s+(\d+)\s+(\S+)")
RE_SESSION = re.compile(r'^Session "(.*)": (.+?) - (.+)$')
# classdumpdir 落盘的文件名形如 Foo$Bar.cc23bf3ed0b8a2fb.class —— 指纹就在文件名里
RE_DUMPED_CLASS = re.compile(r"\.([0-9a-f]{16})\.class$")


def exec_sessions(cfg, execfiles):
    """读出 exec 里的会话信息，返回 [{id, start, dump}]。"""
    if not execfiles:
        return []
    out = run_cli(cfg, ["execinfo"] + list(execfiles), quiet=False)
    sessions = []
    for line in out.splitlines():
        m = RE_SESSION.match(line.strip())
        if m:
            sessions.append({"id": m.group(1), "start": m.group(2).strip(),
                             "dump": m.group(3).strip()})
    return sessions


def exec_class_ids(cfg, execfiles):
    """读出 exec 里记录了哪些 class id，返回 {指纹: (命中, 探针, 类名)}。"""
    if not execfiles:
        return {}
    out = run_cli(cfg, ["execinfo"] + list(execfiles), quiet=False)
    found = {}
    for line in out.splitlines():
        m = RE_EXEC_CLASS.match(line.strip())
        if m:
            found[m.group(1)] = (int(m.group(2)), int(m.group(3)), m.group(4))
    return found


def class_file_ids(cfg, paths):
    """读出 class 产物里有哪些 class id，返回 {指纹: 类名}。

    classdumpdir 的产物文件名自带指纹，直接解析文件名即可，不必起 JVM ——
    对动辄上千个类的服务，这条快路径省下的是几秒到几十秒。
    """
    from_names = {}
    unresolved = []
    for path in paths:
        if not os.path.isdir(path):
            unresolved.append(path)
            continue
        hits = 0
        for dirpath, _, files in os.walk(path):
            for name in files:
                m = RE_DUMPED_CLASS.search(name)
                if m:
                    rel = os.path.relpath(os.path.join(dirpath, name), path)
                    vm = rel.replace("\\", "/")[:-len(".class")]
                    from_names[m.group(1)] = vm[:vm.rfind(".")] if "." in vm else vm
                    hits += 1
        if not hits:
            unresolved.append(path)

    if unresolved:
        out = run_cli(cfg, ["classinfo"] + unresolved, quiet=False)
        for line in out.splitlines():
            m = RE_CLASSINFO.search(line)
            if m:
                from_names[m.group(1)] = m.group(2)
    return from_names


def fingerprint(cfg, svc, paths=None):
    """把 class 产物的指纹集合压成一个短哈希，作为「这一版跑的是哪份代码」的身份。

    比人填的版本号可信：人会填错，class 指纹不会。发布换了代码它必然变，
    没换就必然不变。
    """
    ids = class_file_ids(cfg, paths if paths is not None else svc.get("classfiles", []))
    if not ids:
        return None
    digest = hashlib.sha256(("".join(sorted(ids))).encode("ascii")).hexdigest()
    return digest[:12]


def do_dump(cfg, svc, dest, reset=False):
    args = ["dump", "--address", svc["address"], "--port", str(svc["port"]),
            "--destfile", dest, "--retry", str(svc.get("dumpRetry", 3))]
    if reset:
        args.append("--reset")
    run_cli(cfg, args)
    return dest


def make_report(cfg, svc, execfiles, out_dir, name):
    """生成 HTML + XML + CSV。XML 就是推 SonarQube 用的那份。"""
    classfiles = prepare_classfiles(cfg, svc)
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)

    args = ["report"] + list(execfiles)
    for path in classfiles:
        args += ["--classfiles", path]
    for path in svc.get("sourcefiles", []):
        args += ["--sourcefiles", path]
    args += [
        "--html", os.path.join(out_dir, "html"),
        "--xml", os.path.join(out_dir, "jacoco.xml"),
        "--csv", os.path.join(out_dir, "jacoco.csv"),
        "--name", name,
        "--encoding", svc.get("sourceEncoding", "UTF-8"),
    ]
    run_cli(cfg, args)
    return summarize(os.path.join(out_dir, "jacoco.csv"))


def summarize(csv_path):
    totals = {c: [0, 0] for c in COUNTERS}   # [covered, total]
    classes_total = classes_hit = 0
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            classes_total += 1
            if int(row["INSTRUCTION_COVERED"]) > 0:
                classes_hit += 1
            for c in COUNTERS:
                totals[c][0] += int(row[c + "_COVERED"])
                totals[c][1] += int(row[c + "_COVERED"]) + int(row[c + "_MISSED"])
    out = {c: {"covered": v[0], "total": v[1],
               "pct": (100.0 * v[0] / v[1]) if v[1] else 0.0}
           for c, v in totals.items()}
    out["CLASS"] = {"covered": classes_hit, "total": classes_total,
                    "pct": (100.0 * classes_hit / classes_total) if classes_total else 0.0}
    return out


# --------------------------------------------------------------------------
# 状态记录
# --------------------------------------------------------------------------

def state_path(cfg, svc):
    return os.path.join(svc_dir(cfg, svc), "state.json")


def load_state(cfg, svc):
    path = state_path(cfg, svc)
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError) as exc:
            # 读不出来 = 历史清零 + 会话基线丢失，而采集会照常跑下去、断代检测
            # 从此哑火。静默吞掉是最坏的处理方式，至少得在日志里留下痕迹。
            log("! %s 的 state.json 读取失败，按空状态继续：%s" % (svc["name"], exc))
    return {"service": svc["name"], "history": [], "versions": []}


def save_state(cfg, svc, state):
    """先写临时文件再 os.replace —— state.json 不能有"写了一半"的中间态。

    它一个文件装着 history、versions、breaks 和 sessionStart，直接原地覆写时
    只要在中途断电或被 kill，就会留下半个 JSON；而 load_state 拿不到内容只会
    退回空状态，一声不吭地把历史和断代基线一起丢掉。配置回写早就是这个待遇了
    （见 yaml_update_service），这里跟上。
    """
    path = state_path(cfg, svc)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def update_state(cfg, svc, **fields):
    """只改 state.json 里的若干字段，不动 history。"""
    state = load_state(cfg, svc)
    state.update(fields)
    save_state(cfg, svc, state)
    return state


def record(cfg, svc, summary, kind, version=None, extra=None):
    state = load_state(cfg, svc)
    entry = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "version": version or svc.get("version"),
        "instruction": round(summary["INSTRUCTION"]["pct"], 2),
        "branch": round(summary["BRANCH"]["pct"], 2),
        "covered": summary["INSTRUCTION"]["covered"],
        "total": summary["INSTRUCTION"]["total"],
        "classesHit": summary["CLASS"]["covered"],
        "classesTotal": summary["CLASS"]["total"],
    }
    if extra:
        entry.update(extra)
    state["history"].append(entry)
    state["history"] = state["history"][-500:]
    state["latest"] = entry
    if kind == "predeploy":
        state.setdefault("versions", []).append(entry)
    save_state(cfg, svc, state)
    return entry


# --------------------------------------------------------------------------
# 周期封存与断代检测
# --------------------------------------------------------------------------

def auto_version(cfg, svc):
    """给一个周期取名。优先用配置里的版本号，其次用 class 指纹，最后用时间戳。

    指纹比人填的版本号可信 —— 换了代码它必然变，没换必然不变。
    """
    if svc.get("version"):
        return svc["version"]
    try:
        fp = fingerprint(cfg, svc)
    except Exception:
        fp = None
    return ("fp-" + fp) if fp else datetime.now().strftime("%Y%m%d-%H%M%S")


def _safe_fingerprint(cfg, svc):
    """算 class 指纹，失败不影响归档本身。"""
    try:
        return fingerprint(cfg, svc)
    except Exception as exc:
        log("  ! 指纹计算失败（不影响归档）：%s" % exc)
        return None


def _archive_path(root, version):
    """已存在同名归档时另起一个名字。

    归档里的 exec 是不可再生的执行轨迹，宁可多一个目录，也不能覆盖掉。
    """
    base = os.path.join(root, "versions", version)
    if not os.path.exists(os.path.join(base, "manifest.json")):
        return base
    n = 2
    while os.path.exists(os.path.join("%s-%d" % (base, n), "manifest.json")):
        n += 1
    log("  ! versions/%s 已有归档，本次存为 %s-%d" % (version, version, n))
    return "%s-%d" % (base, n)


def check_data_health(cfg, svc):
    """结算前体检 exec 与 class 指纹对不对得上，返回 diagnose 结果。

    **不阻断结算。** 走到 predeploy 说明服务马上要停，exec 是不可再生的 ——
    因为指纹对不上就拒绝归档，只会让这段数据既对不上、又没留下。
    所以这里只负责把话说清楚，并把结论写进 manifest，日后能追。
    """
    try:
        result = diagnose(cfg, svc)
    except Exception as exc:
        log("  ! 数据体检跳过（不影响归档）：%s" % exc)
        return None

    rate = result.get("matchRate")
    if rate is None:
        return result
    if rate < 50:
        log("  !! 指纹匹配率只有 %.1f%% —— 这一版的报告基本是废的" % rate)
        log("     %s" % result["verdict"])
        log("     exec 照常归档（不可再生），但重出报告前得先把 class 产物对上")
    elif rate < 95:
        log("  ! 指纹匹配率 %.1f%%，报告会偏低 —— %s" % (rate, result["verdict"]))
    else:
        log("  数据体检：指纹匹配 %.1f%%，正常" % rate)
    return result


def archive_cycle(cfg, svc, version, entry, out_dir, execs, reason, health=None):
    """把一个采集周期封存到 versions/<版本>/。

    predeploy（先 dump --reset 再封存）和断代检测（进程已经没了，用手上现有的
    exec 封存）走的是同一段归档动作，抽在这里。
    """
    root = svc_dir(cfg, svc)
    archive = _archive_path(root, version)
    shutil.rmtree(archive, ignore_errors=True)
    shutil.copytree(out_dir, archive)

    exec_archive = os.path.join(archive, "exec")
    os.makedirs(exec_archive, exist_ok=True)
    moved = []
    for path in execs:
        dest = os.path.join(exec_archive, os.path.basename(path))
        shutil.move(path, dest)
        moved.append(dest)

    # 一个版本压成一个 exec：重出报告更快，推 Sonar / 转存归档也只用带一个文件。
    # 原始快照仍然保留 —— 它们各自带着会话信息，是日后取证的依据。
    merged = None
    if moved:
        try:
            merged = os.path.join(archive, "merged.exec")
            run_cli(cfg, ["merge"] + moved + ["--destfile", merged])
        except RuntimeError as exc:
            log("  ! merge 失败，跳过（原始快照不受影响）：%s" % exc)
            merged = None

    with open(os.path.join(archive, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({
            "service": svc["name"], "version": version,
            "sealedAt": entry["at"], "sealedBy": reason, "summary": entry,
            "classfiles": svc["classfiles"],
            "fingerprint": _safe_fingerprint(cfg, svc),
            "execCount": len(moved),
            "matchRate": (health or {}).get("matchRate"),
            "healthVerdict": (health or {}).get("verdict"),
            "merged": os.path.basename(merged) if merged else None,
            "note": "exec 仅对本 manifest 记录的 class 产物有效（JaCoCo 按 CRC64 class id 匹配）",
        }, f, ensure_ascii=False, indent=2)

    # 体检结论跟着这一版的结算记录走，日后查「这版数字能不能信」不用翻 manifest
    if health and health.get("matchRate") is not None:
        state = load_state(cfg, svc)
        if state.get("versions"):
            state["versions"][-1]["matchRate"] = health["matchRate"]
            save_state(cfg, svc, state)

    # 新周期从零开始：会话基线作废，等下一次采集重新认。
    update_state(cfg, svc, sessionStart=None)
    log("  已归档 → %s" % archive)
    return archive


def detect_break(cfg, svc, new_exec):
    """比对 SessionInfo 的启动时刻，判断被测进程在两次采集之间重启过没有。

    重启意味着 agent 随进程消失、计数器归零，上一周期的数据只到最后一次成功
    dump 为止。此刻必须先把旧周期封存 —— 否则新旧两个进程的数据会混进同一个桶，
    而 JaCoCo 不会为此报任何错。

    注意 dump --reset 也会把启动时刻往前推，所以封存时会把基线清空，
    由下一次采集重新认，避免把自己的 reset 误判成重启。
    """
    sessions = exec_sessions(cfg, [new_exec])
    current = sessions[0]["start"] if sessions else None
    if not current:
        return None, None

    state = load_state(cfg, svc)
    prev = state.get("sessionStart")
    if not prev or prev == current:
        return current, None

    root = svc_dir(cfg, svc)
    exec_dir = os.path.join(root, "exec")
    existing = sorted(os.path.join(exec_dir, f)
                      for f in os.listdir(exec_dir) if f.endswith(".exec"))
    if not existing:
        return current, None

    version = auto_version(cfg, svc)
    log("检测到断代：会话启动时刻 %s → %s" % (prev, current))
    log("  被测进程重启过，先结算上一周期为版本 %s" % version)
    summary = make_report(cfg, svc, existing, os.path.join(root, "current"),
                          "%s (%s)" % (svc["name"], version))
    entry = record(cfg, svc, summary, "seal", version,
                   extra={"reason": "restart-detected", "sessionStart": prev})
    archive = archive_cycle(cfg, svc, version, entry, os.path.join(root, "current"),
                            existing, "restart-detected")

    state = load_state(cfg, svc)
    state.setdefault("breaks", []).append({
        "at": entry["at"], "from": prev, "to": current,
        "sealedAs": os.path.basename(archive),
    })
    state["breaks"] = state["breaks"][-50:]
    save_state(cfg, svc, state)
    return current, archive


def detect_push_break(cfg, svc):
    """push 通道的断代检测：在线实例是不是跑着两份不同的 class。

    pull 那套（比对 SessionInfo 的启动时刻）在这里不成立 —— push 是多副本，
    副本各自重启、扩缩容都是常态，照搬过来会把每次扩容都当成一次断代。

    push 下真正会让报告出错的是**滚动发版中途**：新旧副本的数据落进同一批
    exec，对着任何一份 class 产物都只能对上一半，而 JaCoCo 不会为此报任何错。
    所以这里抓的是混版本，不是重启。

    和 pull 的另一个不同是**不自动封存**。两批数据都真实有效，只是分属两个
    版本，「到此为止」的语义不成立；而多副本下自动封存还会凭空造出一堆归档。
    这里只负责把话说清楚、记进 breaks 让看板亮起来，结算仍由 predeploy 驱动。
    """
    if _COLLECTOR is None:
        return None
    mixed = _COLLECTOR.mixed_versions(svc["name"])
    state = load_state(cfg, svc)
    if mixed == bool(state.get("pushMixed")):
        return None                 # 状态没变。一次滚动发版会连着好几轮都成立

    entry = None
    if mixed:
        entry = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "reason": "mixed-versions",
            "instances": len(collector_instances(svc["name"])),
        }
        state.setdefault("breaks", []).append(entry)
        state["breaks"] = state["breaks"][-50:]
        log("  !! 在线实例跑着两份不同的 class —— 多半是滚动发版正在进行")
        log("     这一批 exec 跨了两个版本，对着任一份 class 产物都只能对上一半")
        log("     发版流程里补一次 predeploy，把旧版本先结算掉")
    else:
        log("  实例的 class 已经统一，混版本状态解除")
    state["pushMixed"] = mixed
    save_state(cfg, svc, state)
    return entry


# --------------------------------------------------------------------------
# 诊断
# --------------------------------------------------------------------------

def diagnose(cfg, svc, version=None):
    """回答「为什么我的报告是全红的」。

    把 exec 里记录的 class id 和 classfiles 的 class id 求交集 —— 匹配率低就是
    class 产物对不上，这是接入时最贵、最难查、而且**不会报错**的一个坑。
    """
    root = ensure_dirs(cfg, svc)
    if version:
        archive = os.path.join(root, "versions", version)
        exec_dir = os.path.join(archive, "exec")
        manifest = os.path.join(archive, "manifest.json")
        classfiles = svc["classfiles"]
        if os.path.isfile(manifest):
            with open(manifest, encoding="utf-8") as f:
                classfiles = json.load(f).get("classfiles") or classfiles
    else:
        exec_dir = os.path.join(root, "exec")
        classfiles = svc["classfiles"]

    execs = sorted(os.path.join(exec_dir, f)
                   for f in os.listdir(exec_dir) if f.endswith(".exec")) \
        if os.path.isdir(exec_dir) else []

    result = {
        "service": svc["name"], "version": version or svc.get("version"),
        "execFiles": len(execs), "classfiles": classfiles,
        "sessions": [], "execClasses": 0, "classFileClasses": 0,
        "matched": 0, "matchRate": None, "verdict": None,
        "missingSamples": [], "breaks": load_state(cfg, svc).get("breaks", [])[-5:],
    }
    if not execs:
        result["verdict"] = "还没有任何 exec 数据"
        return result

    result["sessions"] = exec_sessions(cfg, execs)
    in_exec = exec_class_ids(cfg, execs)
    in_class = class_file_ids(cfg, classfiles)
    result["execClasses"] = len(in_exec)
    result["classFileClasses"] = len(in_class)

    matched = set(in_exec) & set(in_class)
    result["matched"] = len(matched)
    rate = (100.0 * len(matched) / len(in_exec)) if in_exec else 0.0
    result["matchRate"] = round(rate, 1)
    result["missingSamples"] = sorted(
        in_exec[i][2] for i in list(set(in_exec) - matched)[:8])

    if not in_exec:
        # 刚 reset 过、或服务起来还没被访问过，都会是这个状态 ——
        # 这不是 class 对不上，别让诊断把人往错的方向引。
        result["matchRate"] = None
        result["verdict"] = ("exec 里没有任何类的执行记录。服务刚重启或刚结算过？"
                             "再不然就是 includes 没匹配到任何类")
    elif not in_class:
        result["verdict"] = "classfiles 里一个 class 都没找到 —— 路径配错了"
    elif rate >= 95:
        result["verdict"] = "正常"
    elif rate >= 50:
        result["verdict"] = "部分对不上，报告会偏低。多半是 class 产物混了版本"
    else:
        result["verdict"] = ("class 产物对不上，报告会几乎全部显示未覆盖。"
                             "最可能的原因：classfiles 指向的是另一次构建的产物")

    starts = {s["start"] for s in result["sessions"]}
    if len(starts) > 1:
        result["verdict"] += "；另外这批 exec 跨了 %d 个进程会话，可能混了重启前后的数据" % len(starts)
    return result


def cmd_diagnose(cfg, args):
    svc = find_service(cfg, args.service)
    r = diagnose(cfg, svc, args.version)

    # 给流水线用：--json 出结构化结果，省得去解析给人看的排版
    if getattr(args, "json", False):
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return

    print("服务        %s%s" % (r["service"], ("  版本 " + r["version"]) if r["version"] else ""))
    print("exec        %d 个快照 · %d 个类" % (r["execFiles"], r["execClasses"]))
    for s in r["sessions"]:
        print('            会话 "%s"  启动于 %s' % (s["id"], s["start"]))
    print("classfiles  %s" % (" ".join(r["classfiles"]) or "(未配置)"))
    print("            %d 个类" % r["classFileClasses"])
    print()
    if r["matchRate"] is not None:
        print("指纹匹配    %d / %d  (%.1f%%)" % (r["matched"], r["execClasses"], r["matchRate"]))
    print("判定        %s" % r["verdict"])
    if r["missingSamples"]:
        print()
        print("exec 里有、classfiles 里找不到的类（样例）：")
        for name in r["missingSamples"]:
            print("            %s" % name)
    if r["breaks"]:
        print()
        print("断代记录（最近 %d 条）：" % len(r["breaks"]))
        for b in r["breaks"]:
            if b.get("sealedAs"):
                print("            %s  %s → %s  已结算为 %s"
                      % (b["at"], b.get("from", "?"), b.get("to", "?"), b["sealedAs"]))
            else:
                print("            %s  在线实例跑着两份不同的 class（%d 个实例），未结算"
                      % (b["at"], b.get("instances", 0)))


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------

def cmd_agent_opts(cfg, args):
    svc = find_service(cfg, args.service)
    print(agent_opts(cfg, svc))


def service_status(cfg, svc):
    """单个服务的状态快照。CLI 表格与 HTTP API 共用同一份数据。"""
    state = load_state(cfg, svc)
    latest = state.get("latest")
    channel = service_channel(svc)
    insts = collector_instances(svc["name"]) if channel == "push" else []
    endpoint = endpoint_label(svc)
    # push 的实例握在收集端进程手上。在别的进程里（比如直接跑 CLI）看不到它们，
    # 那不等于「离线」—— 得如实说不知道，否则会让人以为服务挂了。
    unknown = channel == "push" and _COLLECTOR is None
    return {
        "name": svc["name"],
        "channel": channel,
        "endpoint": "push · 未知（当前进程没有收集端）" if unknown else endpoint,
        "unknown": unknown,
        "instances": [{"peer": c["peer"], "since": c["since"], "last": c["last"]}
                      for c in insts],
        "online": reachable(svc),
        "version": (latest or {}).get("version") or svc.get("version"),
        "classfiles": svc.get("classfiles", []),
        "latest": latest,
    }


def collect_status(cfg, name=None):
    names = [name] if name else [s["name"] for s in cfg["services"]]
    return [service_status(cfg, find_service(cfg, n)) for n in names]


def cmd_status(cfg, args):
    print("%-22s %-8s %-9s %-9s %-10s %s" % ("服务", "连通", "指令%", "分支%", "版本", "最后更新"))
    print("-" * 78)
    for row in collect_status(cfg, args.service):
        latest = row["latest"] or {}
        # push 服务把在线实例数一并显示出来，"连通" 对它来说是「有几个连着」
        if row.get("channel") == "push":
            if row.get("unknown"):
                conn = "?"
            else:
                conn = ("ok(%d)" % len(row["instances"])) if row["online"] else "--"
        else:
            conn = "ok" if row["online"] else "--"
        print("%-22s %-8s %-9s %-9s %-10s %s" % (
            row["name"],
            conn,
            ("%.1f" % latest["instruction"]) if latest else "-",
            ("%.1f" % latest["branch"]) if latest else "-",
            row["version"] or "-",
            latest.get("at", "从未采集"),
        ))


def _snapshot(cfg, svc, reset, kind, version=None):
    ensure_dirs(cfg, svc)
    root = svc_dir(cfg, svc)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    session_start = None
    if service_channel(svc) == "push":
        # 实例是自己连上来的，向每一个各要一次。多副本的数据落成多个 exec，
        # 出报告时一起喂给 cli report，等价于隐式 merge —— 这正是 push 通道
        # 在多副本场景比 pull 省事的地方。
        if _COLLECTOR is None:
            raise RuntimeError("push 通道要求收集端在同一进程里，请用 serve --with-watch 启动")
        log("%s：向 %d 个在线实例取数%s"
            % (svc["name"], len(collector_instances(svc["name"])),
               "（含 --reset）" if reset else ""))
        got = _COLLECTOR.dump_service(cfg, svc, reset=reset)
        if not got:
            # 探活和取数之间实例断开就会走到这儿，属于正常情况，
            # 交给上层记日志跳过，不能是致命错误
            raise RuntimeError("%s 当前没有实例在线，取不到数据" % svc["name"])
        # push 没有「进程重启 = 计数器归零」这个信号（多副本各自重启是常态），
        # 会让报告出错的是滚动发版中途的混版本 —— 那才是这里要抓的
        detect_push_break(cfg, svc)
    else:
        # 先落到暂存位置：得先看清这份数据属于哪个进程，才知道该把它归进哪个周期。
        staging = os.path.join(root, ".incoming.exec")
        log("%s：dump%s" % (svc["name"], "（含 --reset）" if reset else ""))
        do_dump(cfg, svc, staging, reset=reset)

        if not reset:
            # --reset 自己就会把会话启动时刻往前推，只在普通采集时做断代判断，
            # 否则每次 predeploy 都会被自己误判成一次重启。
            session_start, sealed = detect_break(cfg, svc, staging)
            if sealed:
                log("  上一周期已封存，本次数据归入新周期")

        shutil.move(staging, os.path.join(root, "exec", "%s.exec" % ts))

    # 累加视图始终基于该版本周期内的全部 exec
    execs = sorted(
        os.path.join(root, "exec", f)
        for f in os.listdir(os.path.join(root, "exec")) if f.endswith(".exec")
    )
    out_dir = os.path.join(root, "current")
    summary = make_report(cfg, svc, execs, out_dir,
                          "%s (%s)" % (svc["name"], version or svc.get("version", "runtime")))
    entry = record(cfg, svc, summary, kind, version)
    if session_start:
        update_state(cfg, svc, sessionStart=session_start)
    log("  指令 %.1f%%（%d/%d）  分支 %.1f%%  触达类 %d/%d" % (
        summary["INSTRUCTION"]["pct"], summary["INSTRUCTION"]["covered"],
        summary["INSTRUCTION"]["total"], summary["BRANCH"]["pct"],
        summary["CLASS"]["covered"], summary["CLASS"]["total"]))
    return entry, out_dir, execs


def cmd_dump(cfg, args):
    svc = find_service(cfg, args.service)
    if not reachable(svc):
        if service_channel(svc) == "push":
            die("%s 当前没有实例连上来 —— 确认被测端 agent 用的是 "
                "output=tcpclient 且能访问到 collect.advertiseAddress" % svc["name"])
        die("连不上 %s —— 确认服务在跑，且 agent 用的是 output=tcpserver"
            % endpoint_label(svc))
    _snapshot(cfg, svc, reset=False, kind="dump")
    render_dashboard(cfg)


def cmd_predeploy(cfg, args):
    """发版 / 重启前调用：结算当前版本的覆盖率并归档。

    必须在停服之前执行 —— 服务一停，agent 随之消失，数据再也拉不回来。
    """
    svc = find_service(cfg, args.service)
    version = args.version or svc.get("version") or datetime.now().strftime("%Y%m%d-%H%M%S")

    if not reachable(svc):
        msg = "取不到 %s（%s）的数据，无法结算版本 %s" % (
            svc["name"], endpoint_label(svc), version)
        if args.allow_missing:
            log("警告：" + msg + "（--allow-missing，跳过）")
            return
        die(msg + "\n服务已经停了？那这段数据已经丢失。predeploy 必须在停服之前执行。")

    log("结算版本 %s" % version)
    entry, out_dir, execs = _snapshot(cfg, svc, reset=True, kind="predeploy", version=version)
    # 体检要赶在归档之前 —— archive_cycle 会把 exec 移走
    health = check_data_health(cfg, svc)
    archive = archive_cycle(cfg, svc, version, entry, out_dir, execs, "predeploy", health)
    log("  Sonar 可读取：%s" % os.path.join(archive, "jacoco.xml"))
    render_dashboard(cfg)


def cmd_report(cfg, args):
    svc = find_service(cfg, args.service)
    root = ensure_dirs(cfg, svc)
    exec_dir = os.path.join(root, "exec")
    execs = sorted(os.path.join(exec_dir, f)
                   for f in os.listdir(exec_dir) if f.endswith(".exec"))
    if not execs:
        die("%s 还没有任何 exec 数据" % svc["name"])
    summary = make_report(cfg, svc, execs, os.path.join(root, "current"), svc["name"])
    record(cfg, svc, summary, "report")
    log("指令 %.1f%%  分支 %.1f%%" % (summary["INSTRUCTION"]["pct"], summary["BRANCH"]["pct"]))
    render_dashboard(cfg)


# --------------------------------------------------------------------------
# YAML 行级改写
#
# retarget 每次发版都会回写配置。「解析成 dict 再整体 dump」的写法会把注释和排版
# 一起抹掉，而能写注释正是配置换成 YAML 的理由 —— 所以这里只定位目标服务的那几
# 行做替换，其余原文逐字不动。代价是只认缩进块写法，流式 {a: 1} 会直接报错。
# --------------------------------------------------------------------------

_YAML_PLAIN = re.compile(r"^[A-Za-z0-9_./][A-Za-z0-9_./+@=~-]*$")
_YAML_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_.\-]*)\s*:(\s|$)")
_YAML_ITEM = re.compile(r"^(\s*)-(\s|$)")


def _yaml_scalar(value):
    """把标量渲染成 YAML。拿不准就加引号 —— 引号从不会解析错，裸值会。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if not _YAML_PLAIN.match(text):
        return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"')
    if text.lower() in ("true", "false", "null", "yes", "no", "on", "off", "~"):
        return '"%s"' % text
    if text[0].isdigit():
        # 版本号裸写会被读成数字（1.4 → float，1 → int），一律引起来
        return '"%s"' % text
    return text


def _yaml_split_comment(text):
    """切成 (正文, 行尾注释, 注释起始列)。# 在引号里不算注释。"""
    quote = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i], text[i:].strip(), i
    return text, "", 0


def _yaml_append_comment(line, comment, col):
    """把行尾注释接回去，尽量还原它原来的列，读起来才不会错位。"""
    if not comment:
        return line
    return line + " " * max(2, col - len(line)) + comment


def _yaml_value(line):
    """取 `key: value` 里的 value，剥掉行尾注释与引号。"""
    body = _yaml_split_comment(line.rstrip("\r\n"))[0]
    raw = body.partition(":")[2].strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def _yaml_blank(line):
    body = line.strip()
    return not body or body.startswith("#")


def _yaml_key_col(lines, start, stop):
    """这一项里各 key 的起始列。`-` 单独占一行时键从下一行的缩进算起。"""
    m = re.match(r"^(\s*)-(\s*)", lines[start])
    col = len(m.group(1)) + 1 + len(m.group(2))
    if col >= len(lines[start].rstrip("\r\n")):
        for i in range(start + 1, stop):
            if not _yaml_blank(lines[i]):
                return len(lines[i]) - len(lines[i].lstrip())
    return col


def _yaml_item_keys(lines, start, stop, key_col):
    """列出这一项里的顶层 key，返回 [(key, 起始行, 尾后行)]。"""
    hits = []
    for i in range(start, stop):
        if _yaml_blank(lines[i]):
            continue
        text = lines[i].rstrip("\r\n")
        if i != start and len(text) - len(text.lstrip()) != key_col:
            continue          # 嵌套在某个 key 底下的行，不是这一项的 key
        m = _YAML_KEY.match(text[key_col:])
        if m:
            hits.append((m.group(1), i))
    out = []
    for n, (key, ks) in enumerate(hits):
        ke = hits[n + 1][1] if n + 1 < len(hits) else stop
        while ke > ks + 1 and _yaml_blank(lines[ke - 1]):
            ke -= 1           # 块尾的空行/注释留给下一个 key
        out.append((key, ks, ke))
    return out


def _yaml_service_item(lines, service):
    """定位 services 下 name == service 的那一项，返回 (起始行, 尾后行, key 列)。"""
    top = next((i for i, line in enumerate(lines)
                if re.match(r"^services\s*:", line)), None)
    if top is None:
        raise RuntimeError("配置里找不到顶层的 services:")
    end = len(lines)
    for i in range(top + 1, len(lines)):
        if not _yaml_blank(lines[i]) and not lines[i][:1].isspace():
            end = i
            break
    while end > top + 1 and _yaml_blank(lines[end - 1]):
        end -= 1

    starts, item_indent = [], None
    for i in range(top + 1, end):
        m = _YAML_ITEM.match(lines[i])
        if not m:
            continue
        if item_indent is None:
            item_indent = len(m.group(1))
        if len(m.group(1)) == item_indent:
            starts.append(i)
    if not starts:
        raise RuntimeError("services 下没有缩进块写法的列表项，请手工修改配置")

    for n, start in enumerate(starts):
        stop = starts[n + 1] if n + 1 < len(starts) else end
        while stop > start + 1 and _yaml_blank(lines[stop - 1]):
            stop -= 1
        key_col = _yaml_key_col(lines, start, stop)
        for key, ks, _ in _yaml_item_keys(lines, start, stop, key_col):
            if key == "name" and _yaml_value(lines[ks]) == service:
                return start, stop, key_col
    raise RuntimeError("配置里没有名为 %r 的服务" % service)


def yaml_update_service(path, service, updates):
    """就地改写 YAML 配置里某个服务的若干字段，只动这几行。"""
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    lines = text.splitlines(keepends=True)
    nl = "\r\n" if "\r\n" in text else "\n"
    start, stop, key_col = _yaml_service_item(lines, service)
    known = {k: (ks, ke) for k, ks, ke in _yaml_item_keys(lines, start, stop, key_col)}

    plan = []
    for key, value in updates.items():
        ks, ke = known.get(key, (stop, stop))   # 没配过的字段追加到这一项末尾
        plan.append((ks, ke, key, value))
    # 从后往前改，前面几处的行号才不会被前一次替换挪动
    for ks, ke, key, value in sorted(plan, key=lambda p: (p[0], p[2]), reverse=True):
        exists = ks < ke
        prefix = lines[ks][:key_col] if exists and ks == start else " " * key_col
        _, comment, col = (_yaml_split_comment(lines[ks].rstrip("\r\n"))
                           if exists else ("", "", 0))
        list_indent = key_col + 2
        for i in range(ks + 1, ke):
            m = _YAML_ITEM.match(lines[i])
            if m:
                list_indent = len(m.group(1))   # 沿用原有的列表缩进风格
                break
        if isinstance(value, (list, tuple)):
            block = [_yaml_append_comment("%s%s:" % (prefix, key), comment, col)]
            block += ["%s- %s" % (" " * list_indent, _yaml_scalar(v)) for v in value]
        else:
            block = [_yaml_append_comment(
                "%s%s: %s" % (prefix, key, _yaml_scalar(value)), comment, col)]
        lines[ks:ke] = [b + nl for b in block]

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write("".join(lines))
    os.replace(tmp, path)


def json_update_service(path, service, updates):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    hit = next((s for s in raw.get("services", []) if s["name"] == service), None)
    if hit is None:
        raise RuntimeError("配置里没有名为 %r 的服务" % service)
    hit.update(updates)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def cmd_retarget(cfg, args):
    """发版后把配置指向新版本的 class 产物。

    JaCoCo 按 CRC64 class id 匹配数据，class 产物不跟着版本换，新周期采到的 exec
    就和旧 class 对不上，报告全是"未覆盖"。这一步是发版流水线里最容易漏的。

    直接改配置文件原文（而不是回写 load_config 解析后的结果），避免把相对路径
    固化成绝对路径 —— 整个目录要能原样搬到别的机器上。
    """
    path = args.config
    updates = {}
    if args.version:
        updates["version"] = args.version
    if args.classfiles:
        updates["classfiles"] = list(args.classfiles)
    if args.sourcefiles:
        updates["sourcefiles"] = list(args.sourcefiles)

    raw = read_config_file(path)
    if not any(s.get("name") == args.service for s in raw.get("services", [])):
        die("配置里没有名为 %r 的服务" % args.service)
    if updates:
        if config_format(path) == "yaml":
            yaml_update_service(path, args.service, updates)
        else:
            json_update_service(path, args.service, updates)
        raw = read_config_file(path)
    hit = next(s for s in raw["services"] if s.get("name") == args.service)
    log("%s -> version=%s classfiles=%s"
        % (args.service, hit.get("version"), hit.get("classfiles")))


# 采集、结算、改配置都会写 data/ 与配置文件，单进程内一律串行，
# 避免看板 API 与后台轮询同时对同一个服务动手。
_LOCK = threading.RLock()


def watch_once(cfg):
    for svc in cfg["services"]:
        try:
            if not reachable(svc):
                log("%s：离线，跳过" % svc["name"])
                continue
            with _LOCK:
                _snapshot(cfg, svc, reset=False, kind="watch")
        except (Exception, SystemExit) as exc:
            # 也接住 SystemExit：die() 抛的就是它，穿过 except Exception 会把
            # 采集线程静默杀死 —— 守护进程无声停摆是最坏的失败模式。
            # KeyboardInterrupt 不在此列，Ctrl+C 仍然能正常退出。
            log("%s：采集失败 —— %s" % (svc["name"], exc))
    render_dashboard(cfg)


def watch_loop(cfg_path, interval):
    """每轮重新加载配置 —— retarget 换了 classfiles 之后不必重启采集进程。"""
    while True:
        try:
            watch_once(load_config(cfg_path))
        except (Exception, SystemExit) as exc:
            log("轮询失败 —— %s" % exc)
        time.sleep(interval)


def cmd_watch(cfg, args):
    interval = args.interval or cfg.get("watch", {}).get("intervalSeconds", 300)
    log("守护进程启动，每 %d 秒轮询 %d 个目标（Ctrl+C 退出）"
        % (interval, len(cfg["services"])))
    watch_loop(args.config, interval)


# --------------------------------------------------------------------------
# 远程控制 API
#
# 整套方案只需要一个服务端。被测服务所在的机器和发版节点不装 Python、不装 java、
# 不放配置文件，全部通过这些接口驱动 hub 干活 —— 它们只需要 curl。
#
#   GET  /api/health                            存活探测，不需要令牌
#   GET  /api/openapi.json                      接口的 OpenAPI 描述，不需要令牌
#   GET  /api/status[?service=X]                连通性与最新覆盖率（JSON）
#   GET  /api/agent-opts?service=X              应注入的 -javaagent 参数串
#   GET  /api/agent.jar                         下载 jacocoagent.jar
#   GET  /api/diagnose?service=X[&version=V]    诊断 exec 与 class 是否对得上
#   POST /api/dump?service=X                    拉一次快照（累加）
#   POST /api/predeploy?service=X&version=V     结算并归档，停服前调用
#         &allowMissing=1                       目标已离线时不报错
#   POST /api/report?service=X                  用已有 exec 重出报告
#   POST /api/retarget?service=X&version=V      更新 version / classfiles
#         &classfiles=/a,/b
#   POST /api/upload-classes?service=X          上传该版本的 class 产物压缩包
#         &version=V[&retarget=1]               （tar.gz / zip，正文为二进制）
#   GET  /api/classes?service=X&version=V       把该版本的 class 产物打成 tar.gz 回传
#
# 参数可用 query string，也可用 JSON body。配置了 serve.token（或设了环境变量
# COVHUB_TOKEN）时，除 /api/health 外都要带 X-Covhub-Token 头或 ?token=。
# --------------------------------------------------------------------------

def _members_ok(names):
    """压缩包来自流水线，仍按不可信输入处理：绝对路径、跳出目录一律拒绝。"""
    for name in names:
        clean = name.replace("\\", "/")
        if clean.startswith("/") or ".." in clean.split("/") or ":" in clean.split("/")[0][1:2]:
            raise RuntimeError("压缩包里有不安全的路径：%s" % name)


def _common_prefix(names):
    """构建期打包习惯上会带一层顶层目录（coverage-artifacts/），自动剥掉。"""
    tops = {n.replace("\\", "/").split("/")[0] for n in names if n.strip("/")}
    if len(tops) != 1:
        return ""
    top = tops.pop()
    return top + "/" if any(n.replace("\\", "/").startswith(top + "/") for n in names) else ""


def store_classes(cfg, svc, version, blob):
    """把上传的 class 产物解包到 <dataDir>/<service>/artifacts/<version>/。

    有了它，被测服务、发版节点都不必和 hub 共享文件系统：产物 POST 过来即可。
    报告是 hub 出的，class 就必须在 hub 上 —— 且必须是线上跑的那一份。
    """
    ensure_dirs(cfg, svc)
    dest = os.path.join(svc_dir(cfg, svc), "artifacts", version)
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)

    if zipfile.is_zipfile(blob):
        with zipfile.ZipFile(blob) as zf:
            names = zf.namelist()
            _members_ok(names)
            prefix = _common_prefix(names)
            for name in names:
                if name.endswith("/"):
                    continue
                rel = name[len(prefix):] if prefix and name.startswith(prefix) else name
                target = os.path.join(dest, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(name) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
    else:
        with tarfile.open(blob, "r:*") as tf:
            members = [m for m in tf.getmembers() if m.isfile() or m.isdir()]
            _members_ok([m.name for m in members])
            prefix = _common_prefix([m.name for m in members])
            for m in members:
                if not m.isfile():
                    continue
                rel = m.name[len(prefix):] if prefix and m.name.startswith(prefix) else m.name
                target = os.path.join(dest, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tf.extractfile(m)
                if src is None:
                    continue
                with src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)

    count = sum(len([f for f in files if f.endswith(".class")])
                for _, _, files in os.walk(dest))
    log("%s：已接收 %s 的 class 产物 %d 个 -> %s" % (svc["name"], version, count, dest))
    if not count:
        log("  ! 包里一个 .class 都没有，检查打包方式")
    return dest, count


def classes_sources(cfg, svc, version):
    """找出某个版本的 class 产物在 hub 上的位置，返回 [(打包时的顶层名, 目录)]。

    两个来源，按可信度排序：
      1. artifacts/<版本>/ —— 经 upload-classes 传上来的，一定是那次发版的产物
      2. versions/<版本>/manifest.json 里记的 classfiles —— 结算时实际用来出报告的路径

    配置里当前的 classfiles 不算数：它早就跟着新版本改掉了。
    """
    root = svc_dir(cfg, svc)
    uploaded = os.path.join(root, "artifacts", version)
    if os.path.isdir(uploaded) and os.listdir(uploaded):
        return [("", uploaded)]

    manifest = os.path.join(root, "versions", version, "manifest.json")
    if os.path.isfile(manifest):
        try:
            with open(manifest, encoding="utf-8") as f:
                paths = json.load(f).get("classfiles") or []
        except (ValueError, OSError):
            paths = []
        found = [(("cp%d" % i), path) for i, path in enumerate(paths) if os.path.isdir(path)]
        if found:
            # 只有一份时不套目录，解出来直接就是包结构
            return [("", found[0][1])] if len(found) == 1 else found
    return []


def pack_classes(cfg, svc, version, dest):
    """把该版本的 class 产物打成 tar.gz 写到 dest，返回 (class 数, 字节数)。

    发版节点因此不必自己留一份 class 产物：推 Sonar 时从 hub 取回即可。
    """
    sources = classes_sources(cfg, svc, version)
    if not sources:
        raise RuntimeError(
            "hub 上没有 %s 版本 %s 的 class 产物。"
            "该版本发版时没跑过 upload-classes，或结算时用的 classfiles 已经不在了。"
            % (svc["name"], version))

    count = 0
    with tarfile.open(dest, "w:gz") as tf:
        for top, path in sources:
            for dirpath, _, files in os.walk(path):
                for name in files:
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, path).replace("\\", "/")
                    tf.add(full, arcname=("%s/%s" % (top, rel)) if top else rel)
                    if name.endswith(".class"):
                        count += 1
    return count, os.path.getsize(dest)


# 不需要令牌的两条。health 是存活探测，openapi.json 是静态的接口描述（连服务名
# 都不带）—— 网关和 Swagger UI 得能直接拉，否则接不进去。
OPEN_ROUTES = ("/api/health", "/api/openapi.json")


def _token(cfg):
    return os.environ.get("COVHUB_TOKEN") or (cfg.get("serve") or {}).get("token") or ""


# 浏览器里点开报告时带令牌用的 Cookie。报告页里全是相对链接，不可能每条都
# 挂上 ?token=，所以带对一次就种下它，后续静态请求靠它放行。
TOKEN_COOKIE = "covhub_token"


def _token_ok(given, expected):
    """令牌比对。用 compare_digest 而不是 == —— 逐字符短路会泄漏正确的前缀长度。

    比的是 bytes：token 里出现非 ASCII 时 compare_digest 的 str 形式会直接抛错。
    """
    return secrets.compare_digest(str(given or "").encode("utf-8"),
                                  str(expected or "").encode("utf-8"))


def _as_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [p.strip() for p in str(value).split(",") if p.strip()]


def _capture(fn, *a):
    """在锁内执行子命令，把它打印的日志一起回给调用方。

    die() 走的是 SystemExit，这里翻译成 409 —— 让流水线那边非零退出，
    而不是拿到一个"成功"的空响应继续往下走。
    """
    buf = io.StringIO()
    with _LOCK, contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            fn(*a)
            code = 200
        except SystemExit as exc:
            code = 409 if exc.code else 200
        except Exception as exc:
            print("[covhub] 错误：%s" % exc)
            code = 500
    return code, buf.getvalue()


def api_dispatch(cfg_path, method, route, params):
    """返回 (状态码, JSON 可序列化对象)。配置每次重读，retarget 后立即生效。"""
    cfg = load_config(cfg_path)

    if route == "/api/health":
        return 200, {"ok": True, "version": __version__,
                     "services": [s["name"] for s in cfg.get("services", [])]}

    if route == "/api/openapi.json":
        return 200, OPENAPI_SPEC

    if route == "/api/status" and method == "GET":
        name = params.get("service")
        if name and not any(s["name"] == name for s in cfg.get("services", [])):
            return 404, {"ok": False, "error": "配置里没有名为 %r 的服务" % name}
        return 200, {"ok": True, "services": collect_status(cfg, name)}

    name = params.get("service")
    if not name:
        return 400, {"ok": False, "error": "缺少参数 service"}
    if not any(s["name"] == name for s in cfg.get("services", [])):
        return 404, {"ok": False, "error": "配置里没有名为 %r 的服务" % name}
    svc = find_service(cfg, name)

    if route == "/api/agent-opts":
        if method != "GET":
            return 405, {"ok": False, "error": "/api/agent-opts 只接受 GET"}
        return 200, {"ok": True, "service": name, "agentOpts": agent_opts(cfg, svc)}

    if route == "/api/diagnose":
        if method != "GET":
            return 405, {"ok": False, "error": "/api/diagnose 只接受 GET"}
        try:
            with _LOCK:
                return 200, {"ok": True, "diagnose": diagnose(cfg, svc, params.get("version"))}
        except Exception as exc:
            return 500, {"ok": False, "error": str(exc)}

    if method != "POST":
        return 405, {"ok": False, "error": "%s 只接受 POST" % route}

    ns = argparse.Namespace(config=cfg_path, service=name)
    if route == "/api/dump":
        fn = cmd_dump
    elif route == "/api/report":
        fn = cmd_report
    elif route == "/api/predeploy":
        fn = cmd_predeploy
        ns.version = params.get("version")
        ns.allow_missing = str(params.get("allowMissing", "")).lower() in ("1", "true", "yes")
    elif route == "/api/retarget":
        fn = cmd_retarget
        ns.version = params.get("version")
        ns.classfiles = _as_list(params.get("classfiles"))
        ns.sourcefiles = _as_list(params.get("sourcefiles"))
    else:
        return 404, {"ok": False, "error": "未知接口 " + route}

    code, output = _capture(fn, cfg, ns)
    body = {"ok": code == 200, "service": name, "log": output}
    if code == 200:
        body["latest"] = load_state(load_config(cfg_path), svc).get("latest")
    return code, body


def cmd_serve(cfg, args):
    port = args.port or cfg.get("serve", {}).get("port", 8900)
    root = cfg["dataDir"]
    cfg_path = args.config
    os.makedirs(root, exist_ok=True)
    render_dashboard(cfg)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=root, **kw)

        def log_message(self, fmt, *a):
            pass

        def do_GET(self):
            route = self._route()
            if route.startswith("/api/") or route == "/agent.jar":
                return self._api("GET")
            return self._static(super().do_GET)

        def do_HEAD(self):
            route = self._route()
            if route.startswith("/api/") or route == "/agent.jar":
                return self._send(405, {"ok": False, "error": "该接口不支持 HEAD"})
            return self._static(super().do_HEAD)

        def do_POST(self):
            return self._api("POST")

        # ---- 以下是控制 API ----

        def _route(self):
            path = urllib.parse.urlsplit(self.path).path
            return path.rstrip("/") or "/"

        def _params(self):
            query = urllib.parse.urlsplit(self.path).query
            params = {k: v[-1] for k, v in urllib.parse.parse_qs(query).items()}
            length = int(self.headers.get("Content-Length") or 0)
            # 上传接口的正文是二进制压缩包，留给 _upload 自己读，这里绝不能碰
            if length and self._route() != "/api/upload-classes":
                raw = self.rfile.read(length).decode("utf-8", "replace").strip()
                if raw.startswith("{"):
                    try:
                        params.update(json.loads(raw))
                    except ValueError:
                        pass
                elif raw:
                    params.update({k: v[-1] for k, v in urllib.parse.parse_qs(raw).items()})
            return params

        def _cookie_token(self):
            raw = self.headers.get("Cookie") or ""
            for part in raw.split(";"):
                key, _, value = part.strip().partition("=")
                if key == TOKEN_COOKIE:
                    return urllib.parse.unquote(value)
            return ""

        def _authorized(self, current, params):
            expected = _token(current)
            if not expected or self._route() in OPEN_ROUTES:
                return True
            given = (self.headers.get("X-Covhub-Token") or params.get("token")
                     or self._cookie_token() or "")
            return _token_ok(given, expected)

        def _grant(self, expected):
            """令牌带对了：种上 Cookie，再跳回不带令牌的同一地址。

            令牌留在地址栏会被浏览器历史和 Referer 一起带走，所以只让它在
            这一次请求里出现。
            """
            parts = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parts.query)
            query.pop("token", None)
            target = urllib.parse.urlunsplit(
                ("", "", parts.path, urllib.parse.urlencode(query, doseq=True), "")) or "/"
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Set-Cookie", "%s=%s; Path=/; HttpOnly; SameSite=Strict"
                             % (TOKEN_COOKIE, urllib.parse.quote(expected)))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _static(self, serve):
            """dataDir 的静态服务 —— 配了令牌就必须和 /api/ 一起拦。

            这底下不只有报告：artifacts/ 是线上跑的那份字节码（反编译即源码），
            exec/ 是不可再生的执行轨迹，state.json 有全部历史。只护住 /api/
            而把整棵树敞开，等于那道门白装。
            """
            try:
                current = load_config(cfg_path)
            except SystemExit:
                return self._send(500, {"ok": False, "error": "配置文件读取失败"})
            expected = _token(current)
            if not expected:
                return serve()

            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            from_query = query.get("token", [""])[-1]
            if from_query and _token_ok(from_query, expected):
                return self._grant(expected)
            given = (self.headers.get("X-Covhub-Token")
                     or self._cookie_token() or from_query)
            if not _token_ok(given, expected):
                return self._send(401, {
                    "ok": False,
                    "error": "令牌无效或缺失",
                    "hint": "浏览器：在地址后加 ?token=<serve.token>，之后靠 Cookie 放行；"
                            "命令行：带 X-Covhub-Token 头",
                })
            return serve()

        def _send(self, code, payload, ctype="application/json; charset=utf-8"):
            if isinstance(payload, bytes):
                data = payload
            else:
                data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _upload(self, current, params):
            """接收 class 产物压缩包（tar.gz / zip），可选顺手 retarget。

            正文是二进制，不能走 _params()，所以这里单独读 —— 大包直接落盘，
            不整个读进内存。
            """
            name, version = params.get("service"), params.get("version")
            if not name or not version:
                return self._send(400, {"ok": False, "error": "需要参数 service 与 version"})
            if not any(s["name"] == name for s in current.get("services", [])):
                return self._send(404, {"ok": False, "error": "配置里没有名为 %r 的服务" % name})
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return self._send(400, {"ok": False, "error": "请求体为空，用 --data-binary 上传压缩包"})

            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".upload")
            try:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    tmp.write(chunk)
                    remaining -= len(chunk)
                tmp.close()
                svc = find_service(current, name)
                with _LOCK:
                    dest, count = store_classes(current, svc, version, tmp.name)
            except Exception as exc:
                return self._send(400, {"ok": False, "error": str(exc)})
            finally:
                os.unlink(tmp.name)

            body = {"ok": True, "service": name, "version": version,
                    "path": dest, "classes": count}
            # retarget=1：上传完直接把配置指向这份产物，省一次调用
            if str(params.get("retarget", "")).lower() in ("1", "true", "yes"):
                ns = argparse.Namespace(config=cfg_path, service=name, version=version,
                                        classfiles=[dest], sourcefiles=None)
                code, out = _capture(cmd_retarget, current, ns)
                body["retarget"] = out
                if code != 200:
                    body["ok"] = False
                    return self._send(code, body)
            self._send(200, body)

        def _download_classes(self, current, params):
            """把某个版本的 class 产物打包回传。

            推 Sonar 需要 -Dsonar.java.binaries 指向**采集时运行的那份 class**，
            有了这个接口，发版节点不必自己囤一份历史产物。
            """
            name, version = params.get("service"), params.get("version")
            if not name or not version:
                return self._send(400, {"ok": False, "error": "需要参数 service 与 version"})
            if not any(s["name"] == name for s in current.get("services", [])):
                return self._send(404, {"ok": False, "error": "配置里没有名为 %r 的服务" % name})

            svc = find_service(current, name)
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz")
            tmp.close()
            try:
                with _LOCK:
                    count, size = pack_classes(current, svc, version, tmp.name)
                log("%s：回传 %s 的 class 产物 %d 个（%.1f MB）"
                    % (name, version, count, size / 1048576.0))
                with open(tmp.name, "rb") as f:
                    data = f.read()
            except RuntimeError as exc:
                return self._send(404, {"ok": False, "error": str(exc)})
            except Exception as exc:
                return self._send(500, {"ok": False, "error": str(exc)})
            finally:
                os.unlink(tmp.name)

            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition",
                             'attachment; filename="classes-%s-%s.tar.gz"' % (name, version))
            self.send_header("X-Covhub-Classes", str(count))
            self.end_headers()
            self.wfile.write(data)

        def _api(self, method):
            route = self._route()
            try:
                params = self._params()
                current = load_config(cfg_path)
            except SystemExit:
                return self._send(500, {"ok": False, "error": "配置文件读取失败"})
            if not self._authorized(current, params):
                return self._send(401, {"ok": False, "error": "令牌无效或缺失"})

            # agent jar 直接从 hub 下载：被测机器不必预先铺一份，
            # 容器的 initContainer 一条 curl 就能拿到。
            if route in ("/api/agent.jar", "/agent.jar"):
                jar = current["jacocoAgent"]
                if not os.path.isfile(jar):
                    return self._send(404, {"ok": False, "error": "找不到 " + jar})
                with open(jar, "rb") as f:
                    return self._send(200, f.read(), "application/java-archive")

            if route == "/api/upload-classes":
                return self._upload(current, params)

            if route == "/api/classes":
                return self._download_classes(current, params)

            code, body = api_dispatch(cfg_path, method, route, params)
            if route not in OPEN_ROUTES:
                log("%s %s -> %d" % (method, self.path, code))

            # 纯文本模式，方便 shell 里直接 $(curl ...) 取参数串
            if code == 200 and params.get("format") == "text" and "agentOpts" in body:
                return self._send(200, (body["agentOpts"] + "\n").encode("utf-8"),
                                  "text/plain; charset=utf-8")
            self._send(code, body)

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    collect = cfg.get("collect") or {}
    if collect.get("port"):
        global _COLLECTOR
        _COLLECTOR = PushCollector(cfg_path)
        _COLLECTOR.start(int(collect["port"]), collect.get("bindAddress", "0.0.0.0"))
        if not getattr(args, "with_watch", False):
            log("  ! 收集端已起，但没带 --with-watch —— 连上来的实例不会被定时取数")

    if getattr(args, "with_watch", False):
        interval = args.interval or cfg.get("watch", {}).get("intervalSeconds", 300)
        threading.Thread(target=watch_loop, args=(cfg_path, interval), daemon=True).start()
        log("采集线程已启动，每 %d 秒轮询一次" % interval)

    log("covhub %s 已启动： http://127.0.0.1:%d/  （根目录 %s）" % (__version__, port, root))
    log("控制 API： http://127.0.0.1:%d/api/health%s"
        % (port, "" if _token(cfg) else
           "    [未设置 serve.token：写接口与 data/ 整个目录都对外敞开]"))
    with Server(("0.0.0.0", port), Handler) as httpd:
        httpd.serve_forever()


CONFIG_TEMPLATE_YAML = """\
# covhub 配置。相对路径一律相对本文件所在目录解析。
jacocoAgent: ./lib/jacocoagent.jar
jacocoCli: ./lib/jacococli.jar
dataDir: ./data

serve:
  port: 8900
  token: ""                  # 控制 API 的令牌，不配则任何人都能调写接口

watch:
  intervalSeconds: 300       # 轮询间隔，同时是断代时数据丢失的上界

collect:                     # push 通道的收集端，只有配了 port，serve 才会起它
  port: 6400
  bindAddress: 0.0.0.0
  advertiseAddress: 改成被测端能访问到的 hub 地址
  dumpTimeoutSeconds: 20     # 向单个实例取数的上限，卡住的实例等这么久就丢弃

services:
  - name: example-service
    version: "1.0.0"
    channel: pull            # pull：hub 去连 agent；push：agent 连回 hub
    address: 127.0.0.1       # agent 所在机器，hub 连过去拉数据
    port: 6300
    bindAddress: 0.0.0.0     # agent 在被测端监听的地址
    includes:                # 传给 agent，决定是否插桩，改了要重启服务
      - com.example.*
    excludes: []
    classDumpDir: /tmp/covhub-classes/example-service   # 被测端路径
    classfiles:              # 出报告用的 class，必须与运行中的服务同一份产物
      - /path/to/classes
    sourcefiles:             # 可选，配了才能在报告里下钻到源码行
      - /path/to/src/main/java
    reportExcludes:          # 只影响报告口径，随时可改重出报告
      - com/example/**/dto/**
    sourceEncoding: UTF-8
"""


def cmd_init(cfg_path, _args):
    if os.path.exists(cfg_path):
        die("%s 已存在，不覆盖" % cfg_path)
    if config_format(cfg_path) == "yaml":
        with open(cfg_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(CONFIG_TEMPLATE_YAML)
        print("已生成配置模板 %s，按需修改后即可使用。" % cfg_path)
        return
    template = {
        "jacocoAgent": "./lib/jacocoagent.jar",
        "jacocoCli": "./lib/jacococli.jar",
        "dataDir": "./data",
        "serve": {"port": 8900},
        "collect": {"port": 6400, "bindAddress": "0.0.0.0",
                    "advertiseAddress": "改成被测端能访问到的 hub 地址",
                    "dumpTimeoutSeconds": 20},
        "watch": {"intervalSeconds": 300},
        "services": [{
            "name": "example-service",
            "version": "1.0.0",
            "address": "127.0.0.1",
            "port": 6300,
            "bindAddress": "0.0.0.0",
            "includes": ["com.example.*"],
            "excludes": [],
            "classDumpDir": "/tmp/covhub-classes/example-service",
            "classfiles": ["/path/to/classes"],
            "sourcefiles": ["/path/to/src/main/java"],
            "reportExcludes": ["com/example/**/dto/**"],
            "sourceEncoding": "UTF-8"
        }]
    }
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(template, f, ensure_ascii=False, indent=2)
    print("已生成配置模板 %s，按需修改后即可使用。" % cfg_path)


# --------------------------------------------------------------------------
# OpenAPI 文档
#
# 控制面接口的机器可读描述，GET /api/openapi.json 取。和 /api/health 一样**不要
# 令牌** —— 它是静态结构，连服务名都不带，网关、Swagger UI、Apifox 才好直接拉。
#
# 这份 dict 是唯一事实来源，仓库里的 docs/openapi.json 是它的导出产物；
# 改了接口要一并重新导出，命令见 CLAUDE.md 的验证流程。
# --------------------------------------------------------------------------

def _oa_schema(name):
    return {"$ref": "#/components/schemas/" + name}


def _oa_body(name, desc):
    return {"description": desc,
            "content": {"application/json": {"schema": _oa_schema(name)}}}


def _oa_param(name, desc, required=True, example=None):
    p = {"name": name, "in": "query", "required": required,
         "description": desc, "schema": {"type": "string"}}
    if example is not None:
        p["schema"]["example"] = example
    return p


_OA_ERR = {
    "400": _oa_body("Error", "缺少必需参数"),
    "401": _oa_body("Error", "令牌无效或缺失（hub 配了 serve.token 时）"),
    "404": _oa_body("Error", "配置里没有这个服务"),
    "405": _oa_body("Error", "方法不对，见各接口标注"),
    "500": _oa_body("Error", "hub 内部错误"),
}


def _oa_write_responses(extra_404=None):
    """写接口的公共响应。

    409 是这套 API 最要紧的一个约定：它表示**业务上失败了**（典型如 predeploy
    时目标已经离线），调用方必须据此让部署流程停下来，而不是拿着一个 2xx 继续
    往下走 —— 那会静默丢掉一整段不可再生的覆盖率数据。
    """
    out = {
        "200": _oa_body("CommandResult", "执行成功。log 是这次执行打印的日志"),
        "409": _oa_body("CommandResult",
                        "业务失败（目标不可达、数据不满足前提等）。"
                        "部署流程应当就此停下"),
    }
    out.update(_OA_ERR)
    if extra_404:
        out["404"] = _oa_body("Error", extra_404)
    return out


OPENAPI_SPEC = {
    "openapi": "3.0.3",
    "info": {
        "title": "covhub 控制 API",
        "version": __version__,
        "description":
            "JaCoCo 运行期覆盖率的采集与看板。整套方案只有 hub 这一个服务端，"
            "被测机器和发版节点不装 Python、不装 java、不放配置文件，"
            "全部通过这些接口驱动 hub 干活 —— 它们只需要 curl。\n\n"
            "**写接口在 hub 内部串行执行**，返回体里带着这次执行的日志。"
            "**非 2xx 一律表示失败**，调用方应据此让部署流程停下来。\n\n"
            "发版节点上更省事的做法是用 integration/covhub-client.sh 包一层。",
    },
    "servers": [{"url": "/", "description": "hub 自身"}],
    "security": [{"tokenHeader": []}, {"tokenQuery": []}],
    "tags": [
        {"name": "探活", "description": "不需要令牌"},
        {"name": "查询", "description": "只读，不改任何状态"},
        {"name": "采集", "description": "拉数据、出报告，会写 data/"},
        {"name": "发版", "description": "结算、换产物 —— 顺序错了会丢数据"},
        {"name": "产物", "description": "class 产物的上传与取回"},
    ],
    "paths": {},          # 见下方 _oa_paths()
    "components": {
        "securitySchemes": {
            "tokenHeader": {
                "type": "apiKey", "in": "header", "name": "X-Covhub-Token",
                "description": "推荐。令牌走请求头而不是 URL，"
                               "免得被 access log 和 ps 输出记下来",
            },
            "tokenQuery": {
                "type": "apiKey", "in": "query", "name": "token",
                "description": "浏览器里访问看板时用；hub 会种一个 Cookie "
                               "再跳回不带令牌的地址",
            },
        },
        "schemas": {},    # 由 _oa_schemas() 填
    },
}


def _oa_schemas():
    """响应体结构。只描述调用方真正会用到的字段，不追求穷尽。"""
    obj = lambda props, **kw: dict({"type": "object", "properties": props}, **kw)
    S, I, N, B = ({"type": "string"}, {"type": "integer"},
                  {"type": "number"}, {"type": "boolean"})
    arr = lambda items: {"type": "array", "items": items}

    summary = obj({
        "at": dict(S, description="采集时刻", example="2026-09-10T18:05:00"),
        "kind": dict(S, description="哪个动作产生的",
                     enum=["dump", "watch", "predeploy", "report", "seal"]),
        "version": dict(S, description="版本标识"),
        "instruction": dict(N, description="指令覆盖率百分比", example=13.5),
        "branch": dict(N, description="分支覆盖率百分比"),
        "covered": dict(I, description="已执行指令数"),
        "total": dict(I, description="指令总数"),
        "classesHit": dict(I, description="被执行到的类数"),
        "classesTotal": dict(I, description="类总数"),
    }, description="一次采集的结果摘要")

    return {
        "Error": obj({
            "ok": dict(B, example=False),
            "error": dict(S, description="给人看的失败原因"),
        }, required=["ok", "error"]),

        "Health": obj({
            "ok": dict(B, example=True),
            "version": dict(S, description="hub 版本", example=__version__),
            "services": arr(S),
        }),

        "Summary": summary,

        "ServiceStatus": obj({
            "name": S,
            "channel": dict(S, enum=["pull", "push"],
                            description="pull：hub 去连 agent；push：agent 连回 hub"),
            "endpoint": dict(S, description="在哪儿取数的一句话描述",
                             example="10.0.1.7:6300"),
            "online": dict(B, description="pull 是端口连得通；push 是当前有实例连着"),
            "unknown": dict(B, description="push 专有：当前进程没有收集端，"
                                           "说不出在线与否。这不等于离线"),
            "instances": arr(obj({"peer": S, "since": S, "last": S}),),
            "version": S,
            "classfiles": arr(dict(S, description="hub 上的路径")),
            "latest": summary,
        }),

        "StatusResponse": obj({
            "ok": dict(B, example=True),
            "services": arr(_oa_schema("ServiceStatus")),
        }),

        "AgentOpts": obj({
            "ok": dict(B, example=True),
            "service": S,
            "agentOpts": dict(S, description="塞进被测服务 JAVA_TOOL_OPTIONS 的参数串",
                              example="-javaagent:/opt/jacoco/jacocoagent.jar="
                                      "output=tcpserver,address=0.0.0.0,port=6300,"
                                      "includes=com.example.*,sessionid=1.4.3"),
        }),

        "Diagnose": obj({
            "service": S,
            "version": S,
            "execFiles": dict(I, description="本周期已有多少个 exec 快照"),
            "classfiles": arr(S),
            "sessions": arr(obj({
                "id": S,
                "start": dict(S, description="被测进程的启动时刻。它变了就说明重启过"),
                "dump": S,
            })),
            "execClasses": dict(I, description="exec 里记录了多少个类"),
            "classFileClasses": dict(I, description="class 产物里有多少个类"),
            "matched": dict(I, description="两边指纹对得上的类数"),
            "matchRate": dict(N, nullable=True,
                              description="匹配率百分比。低于 95 报告就会偏低，"
                                          "低于 50 基本是废的。null 表示 exec 里"
                                          "还没有任何执行记录，不是对不上"),
            "verdict": dict(S, description="给人看的判定"),
            "missingSamples": arr(dict(S, description="exec 里有、class 产物里没有的类")),
            "breaks": arr(obj({
                "at": S,
                "sealedAs": dict(S, description="pull：重启已被自动结算成这个归档"),
                "reason": dict(S, description="push：mixed-versions 表示在线实例"
                                              "跑着两份不同的 class，未结算"),
            })),
        }, description="回答「为什么我的报告是全红的」"),

        "DiagnoseResponse": obj({
            "ok": dict(B, example=True),
            "diagnose": _oa_schema("Diagnose"),
        }),

        "CommandResult": obj({
            "ok": dict(B, description="false 时同时会是非 2xx"),
            "service": S,
            "log": dict(S, description="这次执行打印的日志，原样回给调用方"),
            "latest": summary,
        }),

        "UploadResult": obj({
            "ok": dict(B, example=True),
            "service": S,
            "version": S,
            "path": dict(S, description="产物在 hub 上的落点"),
            "classes": dict(I, description="包里解出多少个 .class。"
                                           "是 0 就说明打包方式不对"),
            "retarget": dict(S, description="带了 retarget=1 时，那一步的日志"),
        }),
    }


OPENAPI_SPEC["components"]["schemas"] = _oa_schemas()


def _oa_paths():
    """11 个接口。GET 一律只读，POST 一律会写 data/ 或配置文件。"""
    svc = _oa_param("service", "服务名，要和配置里的 name 对上", example="order-service")
    svc_opt = _oa_param("service", "服务名。不给则返回全部服务", required=False)
    ver = _oa_param("version", "版本标识", example="1.4.3")

    def op(tag, summary, desc, params, responses, method="get", security=None):
        o = {"tags": [tag], "summary": summary, "description": desc,
             "parameters": params, "responses": responses}
        if security is not None:
            o["security"] = security
        return {method: o}

    return {
        "/api/health": op(
            "探活", "存活探测",
            "唯一不需要令牌的接口。返回 hub 版本和已配置的服务名列表。",
            [], {"200": _oa_body("Health", "hub 活着")},
            security=[]),

        "/api/openapi.json": op(
            "探活", "本文档",
            "这份 OpenAPI 描述自身。同样不需要令牌 —— 它是静态结构，"
            "不含任何部署信息，方便网关和 Swagger UI 直接拉取。",
            [], {"200": {"description": "OpenAPI 3.0 文档",
                         "content": {"application/json": {"schema": {"type": "object"}}}}},
            security=[]),

        "/api/status": op(
            "查询", "连通性与最新覆盖率",
            "看板上那些数字的 JSON 版。push 服务的 unknown=true 表示"
            "当前进程没有收集端，说不出在线与否 —— 那不等于离线。",
            [svc_opt],
            dict(_OA_ERR, **{"200": _oa_body("StatusResponse", "查询成功")})),

        "/api/agent-opts": op(
            "查询", "取该服务应注入的 -javaagent 参数串",
            "被测服务零侵入接入的入口：把返回的参数串塞进 JAVA_TOOL_OPTIONS 即可，"
            "不改代码不改 pom。加 format=text 直接出纯文本，方便 shell 里 $(curl ...)。",
            [svc, _oa_param("format", "填 text 则返回纯文本而不是 JSON",
                            required=False, example="text")],
            dict(_OA_ERR, **{"200": _oa_body("AgentOpts", "参数串")})),

        "/api/agent.jar": op(
            "查询", "下载 jacocoagent.jar",
            "被测机器不必预先铺一份 agent，容器的 initContainer 一条 curl 就能拿到。"
            "读的是配置里 jacocoAgent 指向的文件 —— 那一项填的是**被测端**路径，"
            "两者不一致时这个接口会 404，但 agent-opts 照样输出正确的参数串。",
            [],
            dict(_OA_ERR, **{
                "200": {"description": "jar 文件",
                        "content": {"application/java-archive":
                                    {"schema": {"type": "string", "format": "binary"}}}},
                "404": _oa_body("Error", "配置里 jacocoAgent 指向的文件在 hub 上不存在"),
            })),

        "/api/diagnose": op(
            "查询", "诊断 exec 与 class 产物是否对得上",
            "**任何覆盖率数字不对劲，先跑这个。** 它把 exec 里记录的 class id 和"
            " classfiles 的 class id 求交集 —— 匹配率低就是 class 产物对不上，"
            "这是接入时最贵、最难查、而且**不会报错**的一个坑（报告只会显示全部未覆盖）。",
            [svc, _oa_param("version", "诊断某个已归档版本，不给则诊断当前周期",
                            required=False)],
            dict(_OA_ERR, **{"200": _oa_body("DiagnoseResponse", "诊断结果")})),

        "/api/dump": op(
            "采集", "拉一次快照并出报告",
            "累加，不清零。采完会重新渲染看板。",
            [svc], _oa_write_responses(), method="post"),

        "/api/report": op(
            "采集", "用已有 exec 重出报告",
            "不碰被测服务，只是拿现有数据重新生成一次报告 —— 改了 reportExcludes 之后用。",
            [svc], _oa_write_responses(), method="post"),

        "/api/predeploy": op(
            "发版", "结算并归档（停服前必须调用）",
            "dump --reset + 归档 + 出终版报告。\n\n"
            "**必须在停服之前执行。** 服务一停 agent 随进程消失，那段覆盖率没有任何"
            "补救手段 —— 所以目标不可达时它**故意报 409**，好让部署流程停下来。"
            "确实要跳过就带 allowMissing=1，但那意味着这段数据已经丢了。",
            [svc, _oa_param("version", "版本标识，不给则取配置里的 version",
                            required=False),
             _oa_param("allowMissing", "填 1 则目标已离线时不报错", required=False)],
            _oa_write_responses(), method="post"),

        "/api/retarget": op(
            "发版", "把配置指向新版本的 class 产物",
            "**发版流水线里最容易漏的一步。** JaCoCo 按 CRC64 class id 匹配，"
            "class 产物不跟着版本换，新周期采到的 exec 就和旧 class 对不上，"
            "报告全是未覆盖。用 upload-classes 带 retarget=1 可以省掉这一次调用。\n\n"
            "YAML 配置只替换目标服务的那几行，注释和排版原样保留。",
            [svc, _oa_param("version", "新版本标识", required=False),
             _oa_param("classfiles", "**hub 上**的 class 产物路径，逗号分隔",
                       required=False, example="./data/order-service/artifacts/1.4.3"),
             _oa_param("sourcefiles", "hub 上的源码路径，逗号分隔", required=False)],
            _oa_write_responses(), method="post"),

        "/api/upload-classes": {"post": {
            "tags": ["产物"],
            "summary": "上传该版本的 class 产物压缩包",
            "description":
                "报告是 hub 出的，所以 class 必须在 hub 上，且必须是**线上跑的那一份**。"
                "有了这个接口，被测机器和发版节点都不必和 hub 共享文件系统。\n\n"
                "产物优先用 agent 的 classdumpdir 落盘那份 —— 它和 exec 的指纹"
                "定义上必然匹配，还包含 Spring AOP、MyBatis 代理这类构建产物里"
                "根本没有的动态类。\n\n"
                "包里习惯带的一层顶层目录会自动剥掉；绝对路径和跳出目录的成员一律拒绝。\n\n"
                "带 retarget=1 则上传完顺手把配置的 classfiles 指向这份产物，"
                "省一次 /api/retarget 调用 —— **发版时推荐这么用**。",
            "parameters": [
                svc, ver,
                _oa_param("retarget", "填 1 则上传后把配置指向这份产物",
                          required=False, example="1"),
            ],
            "requestBody": {
                "required": True,
                "description": "压缩包本体，tar.gz 或 zip。curl 用 --data-binary @file",
                "content": {
                    "application/gzip": {"schema": {"type": "string", "format": "binary"}},
                    "application/zip": {"schema": {"type": "string", "format": "binary"}},
                    "application/octet-stream":
                        {"schema": {"type": "string", "format": "binary"}},
                },
            },
            "responses": dict(_OA_ERR, **{
                "200": _oa_body("UploadResult",
                                "已接收。classes 是包里解出的 .class 数量，"
                                "是 0 就说明打包方式不对"),
                "400": _oa_body("Error",
                                "缺参数、请求体为空、或压缩包里有不安全的路径"),
            }),
        }},

        "/api/classes": op(
            "产物", "取回某个版本的 class 产物",
            "打成 tar.gz 回传。推 Sonar 时 -Dsonar.java.binaries 要的就是"
            "**采集时运行的那份 class** —— 有了它，发版节点不必自己囤历史产物。\n\n"
            "两个来源按可信度排序：先找 upload-classes 传上来的 artifacts/<版本>/，"
            "再找结算时 manifest 里记的 classfiles。都没有就 404。",
            [svc, ver],
            dict(_OA_ERR, **{
                "200": {
                    "description": "tar.gz 包。响应头 X-Covhub-Classes 是 .class 数量",
                    "headers": {
                        "X-Covhub-Classes": {"description": "包里的 .class 数量",
                                             "schema": {"type": "integer"}},
                        "Content-Disposition": {"schema": {"type": "string"}},
                    },
                    "content": {"application/gzip":
                                {"schema": {"type": "string", "format": "binary"}}},
                },
                "404": _oa_body("Error",
                                "hub 上没有该版本的产物 —— 发版时没跑过 "
                                "upload-classes，或结算时用的 classfiles 已经不在了"),
            })),
    }


OPENAPI_SPEC["paths"] = _oa_paths()


# --------------------------------------------------------------------------
# 看板
# --------------------------------------------------------------------------

def _esc(value):
    """转义成可安全插进 HTML 文本或属性的字符串。

    看板上的服务名、版本号、断代记录都不是本地常量 —— version 经
    /api/retarget 从流水线传进来，落进 state.json，再被渲染进 index.html。
    不转义就等于把写接口变成了看板的脚本注入入口，而 index.html 是全组在看的。
    """
    return html_escape("" if value is None else str(value), quote=True)


def _url(value):
    """编码成 URL 的一个路径段，再按 HTML 属性转义。

    报告链接由服务名 / 版本号拼成，这两者都可能带 / # ? —— 只做 HTML 转义
    拦不住它们改变链接指向，得先做百分号编码。
    """
    return _esc(urllib.parse.quote("" if value is None else str(value), safe=""))


def dashboard_rows(cfg):
    """把每个服务整理成看板要用的一行。

    比 status 多算两件运维上真正关心的事：
      · stale —— 最后一次采集离现在太久，说明采集停了（看板上不显示时间差的话，
        没人会去心算「18:16 是多久以前」）
      · breaks —— 有过没结算的重启，那段覆盖率已经丢了，必须显眼
    """
    interval = int((cfg.get("watch") or {}).get("intervalSeconds", 300))
    stale_after = max(interval * 3, 900)
    now = datetime.now()

    rows = []
    for svc in cfg.get("services", []):
        state = load_state(cfg, svc)
        latest = state.get("latest")
        history = state.get("history", [])[-40:]

        age = None
        if latest:
            try:
                age = (now - datetime.fromisoformat(latest["at"])).total_seconds()
            except (ValueError, TypeError):
                age = None

        # 与上一次采集比，看趋势是涨是跌
        delta = None
        pts = [h["instruction"] for h in history if "instruction" in h]
        if len(pts) >= 2:
            delta = pts[-1] - pts[-2]

        channel = service_channel(svc)
        insts = collector_instances(svc["name"]) if channel == "push" else []
        rows.append({
            "name": svc["name"],
            "channel": channel,
            "endpoint": endpoint_label(svc),
            "online": reachable(svc, timeout=1.0),
            "unknown": channel == "push" and _COLLECTOR is None,
            "instances": len(insts),
            "latest": latest,
            "age": age,
            "stale": age is not None and age > stale_after,
            "delta": delta,
            "history": history,
            "versions": state.get("versions", [])[-10:],
            "breaks": state.get("breaks", [])[-3:],
            "hasReport": os.path.isfile(
                os.path.join(svc_dir(cfg, svc), "current", "html", "index.html")),
        })
    return rows


def render_dashboard(cfg):
    html = build_dashboard_html(dashboard_rows(cfg))
    os.makedirs(cfg["dataDir"], exist_ok=True)
    with open(os.path.join(cfg["dataDir"], "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


def human_age(seconds):
    if seconds is None:
        return "从未采集"
    seconds = int(seconds)
    if seconds < 90:
        return "%d 秒前" % seconds
    if seconds < 5400:
        return "%d 分钟前" % (seconds // 60)
    if seconds < 172800:
        return "%d 小时前" % (seconds // 3600)
    return "%d 天前" % (seconds // 86400)


def spark(history, key="instruction", w=240, h=44):
    """内联 SVG 趋势图：面积 + 折线 + 末点。不引入任何前端依赖。"""
    pts = [h[key] for h in history if key in h]
    if len(pts) < 2:
        return '<div class="spark-empty">趋势数据不足（至少要两次采集）</div>'

    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    pad = 4.0
    step = (w - 2) / (len(pts) - 1)

    def xy(i, v):
        return (1 + i * step, h - pad - (v - lo) / span * (h - 2 * pad))

    coords = [xy(i, v) for i, v in enumerate(pts)]
    line = " ".join("%.1f,%.1f" % c for c in coords)
    area = "%s %.1f,%.1f %.1f,%.1f" % (line, coords[-1][0], h, coords[0][0], h)
    last = coords[-1]
    return (
        '<svg class="spark" viewBox="0 0 %d %d" preserveAspectRatio="none" '
        'role="img" aria-label="指令覆盖率趋势，最近 %d 次采集">'
        '<polygon class="spark-area" points="%s"/>'
        '<polyline class="spark-line" points="%s"/>'
        '<circle class="spark-dot" cx="%.1f" cy="%.1f" r="2.6"/>'
        "</svg>" % (w, h, len(pts), area, line, last[0], last[1]))


def _status_pill(r):
    """状态徽章。语义色只给运维状态用 —— 覆盖率高低不着色，见 CSS 注释。"""
    if r["unknown"]:
        return '<span class="pill pill-warn">状态未知</span>'
    if not r["online"]:
        return '<span class="pill pill-bad">离线</span>'
    if r["channel"] == "push":
        return '<span class="pill pill-ok">在线 · %d 个实例</span>' % r["instances"]
    return '<span class="pill pill-ok">在线</span>'


def _card(r):
    latest = r["latest"]
    head = (
        '<div class="card-head">'
        '<div class="card-title"><h2>%s</h2>'
        '<span class="chan chan-%s">%s</span></div>'
        "%s</div>"
        '<div class="endpoint">%s</div>'
        % (_esc(r["name"]), _esc(r["channel"]), _esc(r["channel"]),
           _status_pill(r), _esc(r["endpoint"])))

    if not latest:
        body = ('<div class="empty-body">尚未采集到数据<span>确认 agent 已注入，'
                "再跑一次 dump</span></div>")
    else:
        inst = latest["instruction"]
        delta = ""
        if r["delta"] is not None and abs(r["delta"]) >= 0.05:
            up = r["delta"] > 0
            delta = ('<span class="delta %s">%s%.1f</span>'
                     % ("up" if up else "down", "+" if up else "−", abs(r["delta"])))

        alerts = []
        if r["stale"]:
            alerts.append('<div class="alert alert-warn">最后一次采集在 %s，'
                          "采集可能已经停了 —— 确认 watch 还在跑</div>"
                          % human_age(r["age"]))
        for b in reversed(r["breaks"]):
            at = _esc(b.get("at", "").replace("T", " "))
            if b.get("sealedAs"):
                alerts.append('<div class="alert alert-warn">检测到未结算的重启，'
                              "已自动结算为 <b>%s</b>（%s）</div>"
                              % (_esc(b.get("sealedAs", "?")), at))
            else:
                alerts.append('<div class="alert alert-warn">在线实例跑着两份不同的 '
                              "class（%s）—— 滚动发版中途采到的数据对不上同一份 "
                              "class 产物，发版流程里补一次 predeploy</div>" % at)

        body = (
            '<div class="headline">'
            '<div class="big"><span class="num">%.1f</span><span class="pct">%%</span>%s</div>'
            '<div class="big-label">指令覆盖率</div>'
            "</div>"
            '<div class="meter" role="img" aria-label="指令覆盖率 %.1f%%">'
            '<i style="width:%.2f%%"></i></div>'
            '<dl class="figs">'
            "<div><dt>分支</dt><dd>%.1f<i>%%</i></dd></div>"
            "<div><dt>触达类</dt><dd>%d<i>/%d</i></dd></div>"
            "<div><dt>已执行指令</dt><dd>%s<i>/%s</i></dd></div>"
            "</dl>"
            '<div class="trend">%s</div>'
            '<div class="meta"><span title="%s">%s</span>'
            '<span class="ver">版本 %s</span></div>'
            "%s"
            % (inst, delta, inst, inst,
               latest["branch"], latest["classesHit"], latest["classesTotal"],
               "{:,}".format(latest["covered"]), "{:,}".format(latest["total"]),
               spark(r["history"]),
               _esc(latest["at"].replace("T", " ")), human_age(r["age"]),
               _esc(latest.get("version") or "—"),
               "".join(alerts)))

    links = []
    if r["hasReport"]:
        links.append('<a class="btn" href="%s/current/html/index.html">打开报告</a>'
                     % _url(r["name"]))
        links.append('<a class="btn btn-quiet" href="%s/current/jacoco.xml">jacoco.xml</a>'
                     % _url(r["name"]))
    if r["versions"]:
        vs = "".join('<a class="vtag" href="%s/versions/%s/html/index.html">%s</a>'
                     % (_url(r["name"]), _url(v["version"]), _esc(v["version"]))
                     for v in reversed(r["versions"]))
        links.append('<div class="vers"><span>已结算</span>%s</div>' % vs)

    cls = "card"
    if r["unknown"] or r["stale"] or r["breaks"]:
        cls += " card-attn"
    if not r["online"] and not r["unknown"]:
        cls += " card-down"
    return ('<article class="%s">%s%s<div class="links">%s</div></article>'
            % (cls, head, body, "".join(links)))


def build_dashboard_html(rows):
    # 需要注意的排前面：一屏之内先看见问题，而不是按配置顺序一个个找
    def weight(r):
        return (0 if (not r["online"] and not r["unknown"]) else
                1 if (r["unknown"] or r["stale"] or r["breaks"]) else 2, r["name"])

    ordered = sorted(rows, key=weight)
    online = sum(1 for r in rows if r["online"])
    attn = sum(1 for r in rows if r["stale"] or r["breaks"] or r["unknown"])
    down = sum(1 for r in rows if not r["online"] and not r["unknown"])

    tiles = (
        '<div class="tile"><span class="tv">%d</span><span class="tl">服务</span></div>'
        '<div class="tile"><span class="tv ok">%d</span><span class="tl">在线</span></div>'
        '<div class="tile"><span class="tv %s">%d</span><span class="tl">离线</span></div>'
        '<div class="tile"><span class="tv %s">%d</span><span class="tl">需要注意</span></div>'
        % (len(rows), online, "bad" if down else "", down, "warn" if attn else "", attn))

    cards = "\n".join(_card(r) for r in ordered) or (
        '<div class="blank"><h2>还没有配置任何服务</h2>'
        "<p>在配置文件的 services 里加一条，再跑 "
        "<code>covhub.py dump &lt;服务名&gt;</code>。</p></div>")

    values = {
        "{{GENERATED}}": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "{{VERSION}}": __version__,
        "{{TILES}}": tiles,
        "{{CARDS}}": cards,
    }
    html = DASHBOARD_TEMPLATE
    for token, value in values.items():
        html = html.replace(token, value)
    return html



DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>覆盖率看板 · coverage-hub</title>
<script>
  // 主题在刷新前先落地，避免每 60 秒自动刷新时闪一下白
  try {
    var t = localStorage.getItem("covhub-theme");
    if (t) { document.documentElement.setAttribute("data-theme", t); }
  } catch (e) {}
</script>
<style>
/* hub 可能部署在内网，不引任何外部字体和 CDN —— 系统字体栈 + 内联 SVG 就够了 */
:root {
  color-scheme: light;
  --bg:      #F4F7F7;
  --panel:   #FFFFFF;
  --sunk:    #EDF2F2;
  --ink:     #0E1A1C;
  --muted:   #5B6E71;
  --faint:   #8B9EA1;
  --line:    #DBE3E4;
  --accent:  #0C7A6C;
  --ok:      #2F8A5B;
  --warn:    #A8761A;
  --bad:     #B24234;
  --shadow:  0 1px 2px rgba(14,26,28,.06), 0 10px 24px -18px rgba(14,26,28,.4);
  --mono: ui-monospace, SFMono-Regular, "Cascadia Mono", Consolas, "Liberation Mono", monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB",
          "Microsoft YaHei", "Source Han Sans SC", Roboto, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg:     #0C1213;
    --panel:  #141B1D;
    --sunk:   #101718;
    --ink:    #E7EEEF;
    --muted:  #90A3A6;
    --faint:  #6A7C7F;
    --line:   #222C2E;
    --accent: #46C4B1;
    --ok:     #58BE86;
    --warn:   #D9A63F;
    --bad:    #DE7365;
    --shadow: 0 1px 2px rgba(0,0,0,.5), 0 12px 28px -20px rgba(0,0,0,.9);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg:     #0C1213;
  --panel:  #141B1D;
  --sunk:   #101718;
  --ink:    #E7EEEF;
  --muted:  #90A3A6;
  --faint:  #6A7C7F;
  --line:   #222C2E;
  --accent: #46C4B1;
  --ok:     #58BE86;
  --warn:   #D9A63F;
  --bad:    #DE7365;
  --shadow: 0 1px 2px rgba(0,0,0,.5), 0 12px 28px -20px rgba(0,0,0,.9);
}

* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font-family: var(--sans); font-size: 14px; line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1440px; margin: 0 auto; padding: 0 24px 64px; }

/* ---------- 顶栏 ---------- */
header.top {
  display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap;
  padding: 26px 0 18px; border-bottom: 1px solid var(--line); margin-bottom: 22px;
}
header.top h1 { font-size: 19px; font-weight: 650; margin: 0; letter-spacing: -.01em; }
header.top .ver {
  font-family: var(--mono); font-size: 11px; color: var(--accent);
  border: 1px solid var(--line); border-radius: 3px; padding: 1px 6px;
}
header.top .gen { margin-left: auto; font-size: 12px; color: var(--faint); font-family: var(--mono); }
button.theme {
  background: none; border: 1px solid var(--line); color: var(--muted);
  border-radius: 5px; padding: 4px 10px; font: inherit; font-size: 12px; cursor: pointer;
}
button.theme:hover { color: var(--ink); border-color: var(--muted); }
button.theme:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

/* ---------- 概览 ---------- */
.tiles { display: flex; gap: 28px; flex-wrap: wrap; margin: 0 0 26px; }
.tile { display: flex; align-items: baseline; gap: 8px; }
.tv {
  font-size: 26px; font-weight: 650; letter-spacing: -.02em;
  font-variant-numeric: tabular-nums;
}
.tv.ok { color: var(--ok); } .tv.warn { color: var(--warn); } .tv.bad { color: var(--bad); }
.tl { font-size: 12px; color: var(--muted); }

/* ---------- 卡片 ---------- */
/* min() 是必要的：窄屏上 340px 固定下限会让整页横向溢出 */
.grid { display: grid; gap: 16px;
        grid-template-columns: repeat(auto-fill, minmax(min(340px, 100%), 1fr)); }
.card {
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 18px 18px 14px; box-shadow: var(--shadow);
  display: flex; flex-direction: column; gap: 12px;
}
/* 需要注意的用左边一道色条标出来，不整块染色 —— 免得一屏花掉 */
.card-attn { border-left: 3px solid var(--warn); }
.card-down { border-left: 3px solid var(--bad); }

.card-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.card-title { display: flex; align-items: center; gap: 8px; min-width: 0; }
.card-head h2 {
  font-size: 15px; font-weight: 650; margin: 0; letter-spacing: -.005em;
  overflow-wrap: anywhere;
}
.chan {
  font-family: var(--mono); font-size: 10px; letter-spacing: .06em; text-transform: uppercase;
  color: var(--faint); border: 1px solid var(--line); border-radius: 3px; padding: 0 5px;
}
.pill {
  margin-left: auto; font-size: 11.5px; font-weight: 600; white-space: nowrap;
  border-radius: 999px; padding: 2px 10px; border: 1px solid;
}
.pill-ok   { color: var(--ok);   border-color: var(--ok);   background: color-mix(in srgb, var(--ok) 12%, transparent); }
.pill-warn { color: var(--warn); border-color: var(--warn); background: color-mix(in srgb, var(--warn) 12%, transparent); }
.pill-bad  { color: var(--bad);  border-color: var(--bad);  background: color-mix(in srgb, var(--bad) 12%, transparent); }

.endpoint {
  font-family: var(--mono); font-size: 11.5px; color: var(--faint);
  margin-top: -8px; overflow-wrap: anywhere;
}

/* 主数字。覆盖率高低**不着色** —— 运行期 13% 不等于「差」，
   按阈值标红只会训练人无视颜色。语义色留给离线/过期/断代那些确定的坏事。 */
.headline { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.big { display: flex; align-items: baseline; gap: 4px; }
.big .num {
  font-size: 34px; font-weight: 660; letter-spacing: -.025em; line-height: 1;
  font-variant-numeric: tabular-nums;
}
.big .pct { font-size: 16px; color: var(--muted); font-weight: 600; }
.big-label { font-size: 12px; color: var(--muted); }
.delta {
  font-size: 12px; font-weight: 600; font-variant-numeric: tabular-nums;
  margin-left: 4px; padding: 1px 6px; border-radius: 3px;
}
.delta.up   { color: var(--ok);  background: color-mix(in srgb, var(--ok) 12%, transparent); }
.delta.down { color: var(--muted); background: var(--sunk); }

.meter { height: 6px; border-radius: 999px; background: var(--sunk); overflow: hidden; }
.meter i { display: block; height: 100%; background: var(--accent); border-radius: 999px; }

.figs {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;
  margin: 0; padding: 12px 0; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line);
}
.figs dt { font-size: 11px; color: var(--faint); margin-bottom: 2px; }
.figs dd {
  margin: 0; font-size: 15px; font-weight: 600; font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.figs dd i { font-style: normal; font-size: 11.5px; color: var(--faint); font-weight: 500; }

.trend { min-height: 44px; }
.spark { display: block; width: 100%; height: 44px; }
.spark-area { fill: var(--accent); opacity: .1; }
.spark-line { fill: none; stroke: var(--accent); stroke-width: 1.6;
              stroke-linejoin: round; stroke-linecap: round;
              /* 图是拉伸铺满的，不加这句描边会跟着横向变形 */
              vector-effect: non-scaling-stroke; }
.spark-dot  { fill: var(--accent); vector-effect: non-scaling-stroke; }
.spark-empty { font-size: 11.5px; color: var(--faint); padding: 14px 0; }

.meta {
  display: flex; gap: 12px; justify-content: space-between; align-items: baseline;
  font-size: 11.5px; color: var(--muted); flex-wrap: wrap;
}
.meta .ver { font-family: var(--mono); color: var(--faint); }

.alert {
  font-size: 12px; border-radius: 6px; padding: 8px 10px; line-height: 1.5;
  background: color-mix(in srgb, var(--warn) 10%, transparent);
  color: color-mix(in srgb, var(--warn) 80%, var(--ink));
  border: 1px solid color-mix(in srgb, var(--warn) 30%, transparent);
}
.alert b { font-family: var(--mono); font-weight: 600; }

.empty-body {
  display: flex; flex-direction: column; gap: 2px; padding: 22px 0;
  color: var(--muted); font-size: 13px;
}
.empty-body span { font-size: 11.5px; color: var(--faint); }

.links { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: auto; padding-top: 4px; }
.btn {
  font-size: 12px; font-weight: 600; text-decoration: none; border-radius: 6px;
  padding: 5px 12px; background: var(--accent); color: var(--panel); border: 1px solid var(--accent);
}
.btn:hover { filter: brightness(1.08); }
.btn-quiet { background: none; color: var(--muted); border-color: var(--line); font-weight: 500; }
.btn-quiet:hover { color: var(--ink); border-color: var(--muted); filter: none; }
.btn:focus-visible, .vtag:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

.vers { display: flex; flex-wrap: wrap; gap: 5px; align-items: center; width: 100%; margin-top: 2px; }
.vers > span { font-size: 11px; color: var(--faint); }
.vtag {
  font-family: var(--mono); font-size: 11px; text-decoration: none;
  color: var(--muted); background: var(--sunk); border-radius: 3px; padding: 1px 6px;
}
.vtag:hover { color: var(--ink); }

.blank {
  grid-column: 1 / -1; background: var(--panel); border: 1px dashed var(--line);
  border-radius: 10px; padding: 40px 24px; text-align: center; color: var(--muted);
}
.blank h2 { font-size: 15px; margin: 0 0 6px; color: var(--ink); }
.blank code { font-family: var(--mono); font-size: 12px; background: var(--sunk); padding: 1px 5px; border-radius: 3px; }

footer.foot {
  margin-top: 30px; padding-top: 16px; border-top: 1px solid var(--line);
  font-size: 11.5px; color: var(--faint); display: flex; gap: 14px; flex-wrap: wrap;
}
</style>
</head>
<body>
<div class="wrap">

<header class="top">
  <h1>覆盖率看板</h1>
  <span class="ver">covhub {{VERSION}}</span>
  <span class="gen">{{GENERATED}} · 每 60 秒自动刷新</span>
  <button class="theme" type="button" id="themeBtn">切换主题</button>
</header>

<section class="tiles">{{TILES}}</section>

<main class="grid">
{{CARDS}}
</main>

<footer class="foot">
  <span>数字是运行期真实执行到的代码，不代表测试是否有效</span>
  <span>报告随采集重新生成，exec 与 class 产物是不可再生资产</span>
</footer>

</div>
<script>
  document.getElementById("themeBtn").addEventListener("click", function () {
    var root = document.documentElement;
    var dark = root.getAttribute("data-theme") === "dark" ||
               (!root.getAttribute("data-theme") &&
                window.matchMedia("(prefers-color-scheme: dark)").matches);
    var next = dark ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("covhub-theme", next); } catch (e) {}
  });
</script>
</body>
</html>
"""

# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="covhub", description="通用 JaCoCo 运行期覆盖率采集与看板")
    parser.add_argument("-c", "--config",
                        help="配置文件路径，缺省按 %s 顺序探测"
                             % " / ".join(CONFIG_CANDIDATES))
    parser.add_argument("-V", "--version", action="version",
                        version="covhub %s" % __version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="生成配置模板")
    p.add_argument("--json", action="store_true",
                   help="生成 targets.json（默认生成 YAML，YAML 需要 PyYAML）")

    p = sub.add_parser("agent-opts", help="打印启动时应注入的 -javaagent 参数")
    p.add_argument("service")

    p = sub.add_parser("status", help="查看目标连通性与最新覆盖率")
    p.add_argument("service", nargs="?")

    p = sub.add_parser("dump", help="拉一次快照并出报告（累加）")
    p.add_argument("service")

    p = sub.add_parser("predeploy", help="发版/重启前结算并归档（dump --reset）")
    p.add_argument("service")
    p.add_argument("--version", help="版本标识，缺省取配置里的 version")
    p.add_argument("--allow-missing", action="store_true",
                   help="目标已离线时不报错退出（谨慎：意味着这段数据已丢失）")

    p = sub.add_parser("report", help="用已有 exec 重新出报告")
    p.add_argument("service")

    p = sub.add_parser("diagnose", help="诊断 exec 与 class 产物是否对得上")
    p.add_argument("service")
    p.add_argument("--version", help="诊断某个已归档版本，缺省诊断当前周期")
    p.add_argument("--json", action="store_true", help="输出 JSON，供流水线判断")

    p = sub.add_parser("retarget", help="发版后更新配置里的 version / classfiles")
    p.add_argument("service")
    p.add_argument("--version", help="新版本标识")
    p.add_argument("--classfiles", nargs="+", help="新版本 class 产物路径，可多个")
    p.add_argument("--sourcefiles", nargs="+", help="新版本源码路径，可多个")

    p = sub.add_parser("watch", help="守护进程：定时轮询全部目标")
    p.add_argument("--interval", type=int, help="间隔秒数")

    p = sub.add_parser("serve", help="起 HTTP 服务：看板 + 远程控制 API")
    p.add_argument("--port", type=int)
    p.add_argument("--with-watch", action="store_true",
                   help="同一进程内跑采集轮询，整套方案只需要这一个服务端")
    p.add_argument("--interval", type=int, help="--with-watch 的轮询间隔秒数")

    args = parser.parse_args()

    if args.cmd == "init":
        return cmd_init(args.config or ("targets.json" if args.json else "targets.yaml"), args)

    args.config = resolve_config_path(args.config)
    cfg = load_config(args.config)
    needs = {"agent-opts": ("jacocoAgent",), "status": (), "retarget": ()}.get(
        args.cmd, ("jacocoCli", "jacocoAgent"))
    for key in needs:
        if not os.path.isfile(cfg.get(key, "")):
            die("配置项 %s 指向的文件不存在：%s" % (key, cfg.get(key)))
    os.makedirs(cfg["dataDir"], exist_ok=True)

    handlers = {
        "agent-opts": cmd_agent_opts, "status": cmd_status, "dump": cmd_dump,
        "predeploy": cmd_predeploy, "report": cmd_report, "retarget": cmd_retarget,
        "diagnose": cmd_diagnose,
        "watch": cmd_watch, "serve": cmd_serve,
    }
    try:
        handlers[args.cmd](cfg, args)
    except KeyboardInterrupt:
        print()
        log("已退出")
    except RuntimeError as exc:
        die(str(exc))


if __name__ == "__main__":
    main()
