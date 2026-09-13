"""应用服务层：CLI 与 HTTP 共用的操作入口。

每个操作都是普通参数、普通返回值，不认识 argparse 也不认识 HTTP。失败一律
raise（见 errors.py），由 cli.main() 翻成退出码、HTTP 层翻成状态码。
加子命令 = 在这里加一个函数，两边各接一条路由，别写第二份逻辑。
"""

from datetime import datetime
import os

from .agent import agent_opts as _agent_opts, endpoint_label, reachable, service_channel
from .collector import get_collector, collector_instances
from .config import config_format, find_service, read_config_file
from .config_edit import json_update_service, yaml_update_service
from .cycle import archive_cycle, check_data_health, snapshot
from .dashboard import render_dashboard
from .diagnose import diagnose as _diagnose
from .errors import CovhubError, ServiceNotFound
from .jacoco import make_report
from .layout import ensure_dirs
from .logbuf import log
from .state import load_state, record


def agent_opts(cfg, name):
    return _agent_opts(cfg, find_service(cfg, name))


def service_status(cfg, svc):
    """单个服务的状态快照。CLI 表格与 HTTP API 共用同一份数据。"""
    state = load_state(cfg, svc)
    latest = state.get("latest")
    channel = service_channel(svc)
    insts = collector_instances(svc["name"]) if channel == "push" else []
    endpoint = endpoint_label(svc)
    # push 的实例握在收集端进程手上。在别的进程里（比如直接跑 CLI）看不到它们，
    # 那不等于「离线」—— 得如实说不知道，否则会让人以为服务挂了。
    unknown = channel == "push" and get_collector() is None
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


def status(cfg, name=None):
    names = [name] if name else [s["name"] for s in cfg["services"]]
    return [service_status(cfg, find_service(cfg, n)) for n in names]


def dump(cfg, name):
    """拉一次快照并出报告（累加，不清零）。"""
    svc = find_service(cfg, name)
    if not reachable(svc):
        if service_channel(svc) == "push":
            raise CovhubError("%s 当前没有实例连上来 —— 确认被测端 agent 用的是 "
                              "output=tcpclient 且能访问到 collect.advertiseAddress" % svc["name"])
        raise CovhubError("连不上 %s —— 确认服务在跑，且 agent 用的是 output=tcpserver"
                          % endpoint_label(svc))
    entry, _, _ = snapshot(cfg, svc, reset=False, kind="dump")
    render_dashboard(cfg)
    return entry


def predeploy(cfg, name, version=None, allow_missing=False):
    """发版 / 重启前调用：结算当前版本的覆盖率并归档。

    必须在停服之前执行 —— 服务一停，agent 随之消失，数据再也拉不回来。
    所以目标不可达时**故意报错**（HTTP 409），让部署流程停下来；只有显式
    allow_missing 才跳过。
    """
    svc = find_service(cfg, name)
    version = version or svc.get("version") or datetime.now().strftime("%Y%m%d-%H%M%S")

    if not reachable(svc):
        msg = "取不到 %s（%s）的数据，无法结算版本 %s" % (
            svc["name"], endpoint_label(svc), version)
        if allow_missing:
            log("警告：" + msg + "（--allow-missing，跳过）")
            return None
        raise CovhubError(msg + "\n服务已经停了？那这段数据已经丢失。predeploy 必须在停服之前执行。")

    log("结算版本 %s" % version)
    entry, out_dir, execs = snapshot(cfg, svc, reset=True, kind="predeploy", version=version)
    # 体检要赶在归档之前 —— archive_cycle 会把 exec 移走
    health = check_data_health(cfg, svc)
    archive = archive_cycle(cfg, svc, version, entry, out_dir, execs, "predeploy", health)
    log("  Sonar 可读取：%s" % os.path.join(archive, "jacoco.xml"))
    render_dashboard(cfg)
    return entry


def report(cfg, name):
    """用已有 exec 重出报告（改了 reportExcludes 后用）。"""
    svc = find_service(cfg, name)
    root = ensure_dirs(cfg, svc)
    exec_dir = os.path.join(root, "exec")
    execs = sorted(os.path.join(exec_dir, f)
                   for f in os.listdir(exec_dir) if f.endswith(".exec"))
    if not execs:
        raise CovhubError("%s 还没有任何 exec 数据" % svc["name"])
    summary = make_report(cfg, svc, execs, os.path.join(root, "current"), svc["name"])
    entry = record(cfg, svc, summary, "report")
    log("指令 %.1f%%  分支 %.1f%%" % (summary["INSTRUCTION"]["pct"], summary["BRANCH"]["pct"]))
    render_dashboard(cfg)
    return entry


def diagnose(cfg, name, version=None):
    return _diagnose(cfg, find_service(cfg, name), version)


def retarget(cfg, name, version=None, classfiles=None, sourcefiles=None):
    """发版后把配置指向新版本的 class 产物。

    JaCoCo 按 CRC64 class id 匹配数据，class 产物不跟着版本换，新周期采到的 exec
    就和旧 class 对不上，报告全是"未覆盖"。这一步是发版流水线里最容易漏的。

    直接改配置文件原文（而不是回写 load_config 解析后的结果），避免把相对路径
    固化成绝对路径 —— 整个目录要能原样搬到别的机器上。
    """
    path = cfg["configPath"]
    updates = {}
    if version:
        updates["version"] = version
    if classfiles:
        updates["classfiles"] = list(classfiles)
    if sourcefiles:
        updates["sourcefiles"] = list(sourcefiles)

    raw = read_config_file(path)
    if not any(s.get("name") == name for s in raw.get("services", [])):
        raise ServiceNotFound("配置里没有名为 %r 的服务" % name)
    if updates:
        if config_format(path) == "yaml":
            yaml_update_service(path, name, updates)
        else:
            json_update_service(path, name, updates)
        raw = read_config_file(path)
    hit = next(s for s in raw["services"] if s.get("name") == name)
    log("%s -> version=%s classfiles=%s" % (name, hit.get("version"), hit.get("classfiles")))
    return {"version": hit.get("version"), "classfiles": hit.get("classfiles")}
