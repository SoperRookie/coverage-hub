"""JaCoCo exec 二进制格式与 remote control 协议。"""

import struct

from .logbuf import log

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


def read_exec_file(path):
    """把一个 .exec 文件读成 (sessions, execdata)，与 write_exec_file 互逆。

    读写往返后应与原文件字节级一致 —— tests/test_exec_format.py 就是拿这个
    比对来守住格式常量的。
    """
    sessions, execdata = [], []
    with open(path, "rb") as f:
        reader = ExecReader(f)
        while True:
            try:
                block = reader.u8()
            except EOFError:
                return sessions, execdata
            if block == BLOCK_HEADER:
                magic, version = reader.u16(), reader.u16()
                if magic != EXEC_MAGIC:
                    raise RuntimeError("%s 不是 JaCoCo exec 文件（magic 0x%04x）" % (path, magic))
                if version != EXEC_VERSION:
                    log("  ! %s 的 exec 格式版本是 0x%04x，本工具按 0x%04x 解析"
                        % (path, version, EXEC_VERSION))
            elif block == BLOCK_SESSIONINFO:
                sessions.append((reader.utf(), reader.i64(), reader.i64()))
            elif block == BLOCK_EXECUTIONDATA:
                execdata.append((reader.i64(), reader.utf(), reader.bools()))
            else:
                raise RuntimeError("%s 里有未知块类型 0x%02x" % (path, block))


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
