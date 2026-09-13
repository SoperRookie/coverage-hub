"""agent 参数串、通道判断、连通性探活。"""

import socket

from .errors import CovhubError
from .collector import collector_instances

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
            raise CovhubError("服务 %s 用的是 push 通道，需要配置 collect.advertiseAddress"
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
def service_channel(svc):
    return (svc.get("channel") or "pull").lower()
def endpoint_label(svc):
    """一句话描述这个服务在哪儿取数。push 服务没有 address/port，别直接摸那两个字段。"""
    if service_channel(svc) == "push":
        return "push · %d 个实例" % len(collector_instances(svc["name"]))
    return "%s:%d" % (svc["address"], svc["port"])
