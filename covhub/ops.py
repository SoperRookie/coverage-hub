"""应用服务层：CLI 与 HTTP 共用的操作入口。

每个操作都是普通参数、普通返回值，不认识 argparse 也不认识 HTTP。失败一律
raise（见 errors.py），由 cli.main() 翻成退出码、HTTP 层翻成状态码。
加子命令 = 在这里加一个函数，两边各接一条路由，别写第二份逻辑。
"""

from datetime import datetime
import os

from pydantic import ValidationError

from .agent import agent_opts as _agent_opts, endpoint_label, reachable, service_channel
from .collector import get_collector, collector_instances
from .config import find_service
from .cycle import archive_cycle, check_data_health, snapshot
from .dashboard import render_dashboard
from .db import importer, repo
from .diagnose import diagnose as _diagnose
from .errors import CovhubError
from .jacoco import make_report
from .layout import ensure_dirs
from .logbuf import log
from .schemas import ServicePatch, ServiceSpec
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

    路径存原文（不展开成绝对路径）—— 整个目录要能原样搬到别的机器上。
    """
    find_service(cfg, name)
    fields = {}
    if version:
        fields["version"] = str(version)
    if classfiles:
        fields["classfiles"] = list(classfiles)
    if sourcefiles:
        fields["sourcefiles"] = list(sourcefiles)
    hit = repo.update_service(name, fields) if fields else repo.get_service(name)
    log("%s -> version=%s classfiles=%s" % (name, hit.get("version"), hit.get("classfiles")))
    return {"version": hit.get("version"), "classfiles": hit.get("classfiles")}


# ---- 服务配置的增删改查 ----

def service_list(cfg):
    # 给人看的是入库原文（相对路径不展开）；运行时展开过的那份在 cfg["services"]
    return repo.list_services()


def service_get(cfg, name):
    return repo.get_service(name)


def service_add(cfg, fields):
    """fields 是 camelCase 的 dict（来自 CLI 参数、--from-file 或请求体）。"""
    spec = _validate(ServiceSpec, fields)
    row = repo.add_service(spec.to_fields())
    log("已登记服务 %s（%s）" % (row["name"], row.get("channel", "pull")))
    return row


def service_replace(cfg, name, fields):
    fields = dict(fields, name=name)
    spec = _validate(ServiceSpec, fields)
    row = repo.replace_service(name, spec.to_fields())
    log("已整份替换服务 %s 的配置" % name)
    return row


def service_update(cfg, name, fields):
    patch = _validate(ServicePatch, fields).to_fields()
    if not patch:
        raise CovhubError("没有给任何要修改的字段")
    # 改完必须仍是一条合法配置（比如把 pull 服务的 address 清掉）
    merged = dict(repo.get_service(name))
    merged.pop("id", None)
    merged.update(patch)
    _validate(ServiceSpec, merged)
    row = repo.update_service(name, patch)
    log("已更新服务 %s：%s" % (name, ", ".join(sorted(patch))))
    return row


def service_remove(cfg, name):
    repo.remove_service(name)
    log("已删除服务 %s 的配置（data/%s/ 里的采集数据未动，需要的话手工处理）" % (name, name))


def _validate(model, fields):
    try:
        return model(**fields)
    except ValidationError as exc:
        # pydantic 的报错给机器看的成分太多，只留「字段：原因」
        parts = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err.get("loc", ())) or "配置"
            msg = err.get("msg", "")
            if msg.startswith("Value error, "):
                msg = msg[len("Value error, "):]
            parts.append("%s：%s" % (loc, msg))
        raise CovhubError("服务配置不合法 —— " + "；".join(parts))


def import_legacy(cfg, config_path, dry_run=False, overwrite=False):
    """把旧 targets.yaml 的 services 导进数据库。"""
    log("从 %s 导入服务配置%s" % (config_path, "（试运行）" if dry_run else ""))
    services = importer.import_services(config_path, dry_run=dry_run, overwrite=overwrite)
    counts = {}
    for outcome in services.values():
        counts[outcome] = counts.get(outcome, 0) + 1
    log("服务：%s" % (", ".join("%s %d" % kv for kv in sorted(counts.items())) or "无"))
    return {"services": services}
