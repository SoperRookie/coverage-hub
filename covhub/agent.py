"""agent 参数串、通道判断、连通性探活。"""

import socket

from .errors import CovhubError
from .collector import collector_instances

# --------------------------------------------------------------------------
# 与 agent / CLI 交互
# --------------------------------------------------------------------------

# 挂 covhub-agent 时要从插桩里排除的两样：它自己的包，和它调用的 JaCoCo 公开入口
# （org.jacoco.agent.rt.RT —— JaCoCo 只自动跳过自己的 internal 包，这个类是 covhub-agent
# 用到时才加载的，照样会被插桩）。不排除的话它们混进 exec 和 classdumpdir，用 classDumpDir
# 出报告时还会出现在报告里。实测过：includes 缺省时两个类都在 dump 结果里。
COVHUB_AGENT_EXCLUDES = ("covhub.agent.*", "org.jacoco.agent.rt.*")


def agent_opts(cfg, svc):
    """生成 -javaagent 参数串。通用做法：塞进 JAVA_TOOL_OPTIONS 环境变量。

    push 通道且配了 covhubAgent 时是**空格隔开的两个** -javaagent：JaCoCo 只插桩不联网
    （output=none），连 hub、断线重连交给 covhub-agent.jar（agent/ 下的源码说了为什么）。
    没配 covhubAgent 的老部署原样是 output=tcpclient。
    """
    excludes = list(svc.get("excludes") or [])
    thin = None
    if service_channel(svc) == "push":
        # agent 主动连回 hub。address 必须是**被测端能访问到的** hub 地址，
        # 不是 hub 自己的监听地址 —— 跨网段、容器里最容易在这儿配错。
        collect = cfg.get("collect") or {}
        addr = collect.get("advertiseAddress")
        if not addr:
            raise CovhubError("服务 %s 用的是 push 通道，需要配置 collect.advertiseAddress"
                "（被测端连回 hub 用的地址）" % svc["name"])
        endpoint = ["address=%s" % addr, "port=%d" % collect.get("port", 6400)]
        if cfg.get("covhubAgent"):
            # idle：这么久没收到 hub 的指令就当连接已死、重连。hub 每轮轮询都会来取数，
            # 给三轮的余量；轮询间隔改大了最多是多连几次，不丢数据
            interval = int((cfg.get("watch") or {}).get("intervalSeconds", 300))
            thin = "-javaagent:%s=%s" % (cfg["covhubAgent"], ",".join(
                endpoint + ["idle=%d" % max(interval * 3, 180)]))
            opts = ["output=none"]
            excludes.extend(COVHUB_AGENT_EXCLUDES)
        else:
            opts = ["output=tcpclient"] + endpoint
    else:
        opts = [
            "output=tcpserver",
            "address=%s" % svc.get("bindAddress", "0.0.0.0"),
            "port=%d" % svc["port"],
        ]
    if svc.get("includes"):
        opts.append("includes=" + ":".join(svc["includes"]))
    if excludes:
        opts.append("excludes=" + ":".join(excludes))
    if svc.get("classDumpDir"):
        # 让 agent 把它实际加载到的 class 落盘。这份 class 与 exec 的 class id
        # 不是「应该匹配」，是定义上必然匹配 —— 出报告时用它，不会再有全红。
        opts.append("classdumpdir=%s" % svc["classDumpDir"])
    # push 通道靠 sessionid 认领连接，必须是服务名；pull 通道沿用版本号做标记
    opts.append("sessionid=%s" % (svc["name"] if service_channel(svc) == "push"
                                  else svc.get("version", svc["name"])))
    jacoco = "-javaagent:%s=%s" % (cfg["jacocoAgent"], ",".join(opts))
    # JaCoCo 在前：轮到 covhub-agent 时它已经就绪（顺序反了也只是多等一秒，不是错）
    return jacoco if thin is None else jacoco + " " + thin


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
