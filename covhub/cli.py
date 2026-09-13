"""命令行入口。

这里只做三件事：解析参数、调 ops、打印。die() 只允许在这个模块出现 ——
库函数抛 CovhubError，由 main() 统一翻成退出码。
"""

import argparse
import json
import os
import sys

from . import __version__, ops
from .config import (CONFIG_CANDIDATES, CONFIG_TEMPLATE_JSON, CONFIG_TEMPLATE_YAML,
                     SERVICE_TEMPLATE_YAML, config_format, describe_database_url, load_config,
                     parse_yaml, resolve_config_path, resolve_database_url)
from .db import migrate
from .errors import CovhubError
from .httpd import cmd_serve
from .logbuf import log
from .runtime import load_runtime
from .watch import watch_loop


def die(msg, code=1):
    print("[covhub] %s" % msg, file=sys.stderr)
    sys.exit(code)


def cmd_diagnose(cfg, args):
    r = ops.diagnose(cfg, args.service, args.version)

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



def cmd_agent_opts(cfg, args):
    print(ops.agent_opts(cfg, args.service))


def cmd_status(cfg, args):
    print("%-22s %-8s %-9s %-9s %-10s %s" % ("服务", "连通", "指令%", "分支%", "版本", "最后更新"))
    print("-" * 78)
    for row in ops.status(cfg, args.service):
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


def cmd_dump(cfg, args):
    ops.dump(cfg, args.service)


def cmd_predeploy(cfg, args):
    ops.predeploy(cfg, args.service, version=args.version, allow_missing=args.allow_missing)


def cmd_report(cfg, args):
    ops.report(cfg, args.service)


def cmd_retarget(cfg, args):
    ops.retarget(cfg, args.service, version=args.version,
                 classfiles=args.classfiles, sourcefiles=args.sourcefiles)


def cmd_watch(cfg, args):
    interval = args.interval or cfg.get("watch", {}).get("intervalSeconds", 300)
    log("守护进程启动，每 %d 秒轮询 %d 个目标（Ctrl+C 退出）"
        % (interval, len(cfg["services"])))
    watch_loop(args.config, interval)


def cmd_init(cfg_path, args):
    if os.path.exists(cfg_path):
        die("%s 已存在，不覆盖" % cfg_path)
    if config_format(cfg_path) == "yaml":
        with open(cfg_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(CONFIG_TEMPLATE_YAML)
    else:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(CONFIG_TEMPLATE_JSON, f, ensure_ascii=False, indent=2)
    print("已生成配置模板 %s，按需修改后即可使用。" % cfg_path)
    print("服务配置在数据库里：covhub service add <name> ... 登记，或 covhub import 导入旧的 targets.yaml。")


# ---- 服务配置 ----

SERVICE_FLAGS = (
    # (命令行参数, 字段名, 是否列表)
    ("--version", "version", False), ("--channel", "channel", False),
    ("--address", "address", False), ("--port", "port", False),
    ("--bind-address", "bindAddress", False), ("--includes", "includes", True),
    ("--excludes", "excludes", True), ("--class-dump-dir", "classDumpDir", False),
    ("--classfiles", "classfiles", True), ("--sourcefiles", "sourcefiles", True),
    ("--report-excludes", "reportExcludes", True),
    ("--source-encoding", "sourceEncoding", False), ("--dump-retry", "dumpRetry", False),
)


def _add_service_flags(p):
    for flag, field, is_list in SERVICE_FLAGS:
        if is_list:
            p.add_argument(flag, nargs="+", dest=field, metavar="X", help="%s（可多个）" % field)
        elif field in ("port", "dumpRetry"):
            p.add_argument(flag, type=int, dest=field)
        else:
            p.add_argument(flag, dest=field)
    p.add_argument("--from-file", metavar="FILE",
                   help="从 YAML / JSON 文件读取整条配置（命令行参数覆盖文件里的同名字段）")


def _service_fields(args, cfg):
    fields = {}
    if args.from_file:
        with open(args.from_file, encoding="utf-8") as f:
            text = f.read()
        data = parse_yaml(text, args.from_file) if config_format(args.from_file) == "yaml" \
            else json.loads(text)
        if not isinstance(data, dict):
            die("%s 的顶层必须是一个对象" % args.from_file)
        fields.update(data)
    for _flag, field, _is_list in SERVICE_FLAGS:
        value = getattr(args, field, None)
        if value is not None:
            fields[field] = value
    # 相对路径存原文、相对配置文件目录解析；在别的目录里敲命令的人容易以为
    # 是相对当前目录，提示一句
    for key in ("classfiles", "sourcefiles"):
        for path in fields.get(key) or []:
            if not os.path.isabs(path):
                log("  %s 里的相对路径 %s 将相对 %s 解析" % (key, path, cfg["baseDir"]))
    return fields


def _print_service(row):
    print(json.dumps(row, ensure_ascii=False, indent=2))


def cmd_service(cfg, args):
    if args.action == "list":
        rows = ops.service_list(cfg)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return
        print("%-22s %-6s %-22s %-10s %s" % ("服务", "通道", "端点", "版本", "classfiles"))
        print("-" * 90)
        for r in rows:
            endpoint = "%s:%s" % (r.get("address", "-"), r.get("port", "-")) \
                if r.get("channel", "pull") == "pull" else "-"
            print("%-22s %-6s %-22s %-10s %s" % (
                r["name"], r.get("channel", "pull"), endpoint, r.get("version") or "-",
                ", ".join(r.get("classfiles") or []) or "-"))
        return
    if args.action == "show":
        return _print_service(ops.service_get(cfg, args.name))
    if args.action == "add":
        fields = _service_fields(args, cfg)
        fields["name"] = args.name
        return _print_service(ops.service_add(cfg, fields))
    if args.action == "update":
        return _print_service(ops.service_update(cfg, args.name, _service_fields(args, cfg)))
    if args.action == "remove":
        if not args.yes:
            die("删除服务配置需要加 --yes 确认（data/%s/ 里的采集数据不会被删）" % args.name)
        return ops.service_remove(cfg, args.name)
    if args.action == "template":
        print(SERVICE_TEMPLATE_YAML, end="")


def cmd_import(cfg, args):
    ops.import_legacy(cfg, args.source or cfg["configPath"],
                      dry_run=args.dry_run, overwrite=args.overwrite)


def cmd_db(cfg, args):
    url = resolve_database_url(cfg)
    if args.action == "upgrade":
        migrate.upgrade(url, args.revision)
        print("数据库结构已升到 %s（%s）" % (migrate.current(url), describe_database_url(url)))
    elif args.action == "current":
        print("当前 %s，最新 %s（%s）" % (migrate.current(url) or "空库", migrate.head(),
                                       describe_database_url(url)))
    elif args.action == "revision":
        migrate.revision(url, args.message, autogenerate=not args.empty)


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
                   help="生成 covhub.json（默认生成 YAML，YAML 需要 PyYAML）")

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

    p = sub.add_parser("retarget", help="发版后更新服务的 version / classfiles")
    p.add_argument("service")
    p.add_argument("--version", help="新版本标识")
    p.add_argument("--classfiles", nargs="+", help="新版本 class 产物路径，可多个")
    p.add_argument("--sourcefiles", nargs="+", help="新版本源码路径，可多个")

    p = sub.add_parser("service", help="服务配置的增删改查（存数据库）")
    sp = p.add_subparsers(dest="action", required=True)
    q = sp.add_parser("list", help="列出全部服务")
    q.add_argument("--json", action="store_true")
    q = sp.add_parser("show", help="查看一条服务的完整配置")
    q.add_argument("name")
    q = sp.add_parser("add", help="登记一条服务")
    q.add_argument("name")
    _add_service_flags(q)
    q = sp.add_parser("update", help="修改服务的若干字段")
    q.add_argument("name")
    _add_service_flags(q)
    q = sp.add_parser("remove", help="删除服务配置（不删采集数据）")
    q.add_argument("name")
    q.add_argument("--yes", action="store_true")
    sp.add_parser("template", help="打印 --from-file 用的 YAML 模板")

    p = sub.add_parser("import", help="把旧 targets.yaml 里的 services 导入数据库")
    p.add_argument("source", nargs="?", help="旧配置文件路径，缺省用 -c 指向的那个")
    p.add_argument("--dry-run", action="store_true", help="只报告会做什么，不写库")
    p.add_argument("--overwrite", action="store_true", help="同名服务已存在时覆盖")

    p = sub.add_parser("db", help="数据库结构维护")
    sp = p.add_subparsers(dest="action", required=True)
    q = sp.add_parser("upgrade", help="升级表结构到指定版本（默认最新）")
    q.add_argument("revision", nargs="?", default="head")
    sp.add_parser("current", help="查看当前表结构版本")
    q = sp.add_parser("revision", help="（开发用）生成一份迁移脚本")
    q.add_argument("-m", "--message", required=True)
    q.add_argument("--empty", action="store_true", help="不自动比对模型，生成空脚本")

    p = sub.add_parser("watch", help="守护进程：定时轮询全部目标")
    p.add_argument("--interval", type=int, help="间隔秒数")

    p = sub.add_parser("serve", help="起 HTTP 服务：看板 + 远程控制 API")
    p.add_argument("--port", type=int)
    p.add_argument("--with-watch", action="store_true",
                   help="同一进程内跑采集轮询，整套方案只需要这一个服务端")
    p.add_argument("--interval", type=int, help="--with-watch 的轮询间隔秒数")

    args = parser.parse_args()

    if args.cmd == "init":
        return cmd_init(args.config or ("covhub.json" if args.json else "covhub.yaml"), args)

    args.config = resolve_config_path(args.config)
    try:
        if args.cmd == "db":
            # 结构维护命令自己管升级，不能被 load_runtime 的自动升级 / 校验抢先
            cfg = load_config(args.config)
        else:
            cfg = load_runtime(args.config)
    except CovhubError as exc:
        die(str(exc))
    needs = {"agent-opts": ("jacocoAgent",), "status": (), "retarget": (), "service": (),
             "import": (), "db": ()}.get(args.cmd, ("jacocoCli", "jacocoAgent"))
    for key in needs:
        if not os.path.isfile(cfg.get(key, "")):
            die("配置项 %s 指向的文件不存在：%s" % (key, cfg.get(key)))
    os.makedirs(cfg["dataDir"], exist_ok=True)

    handlers = {
        "agent-opts": cmd_agent_opts, "status": cmd_status, "dump": cmd_dump,
        "predeploy": cmd_predeploy, "report": cmd_report, "retarget": cmd_retarget,
        "diagnose": cmd_diagnose, "service": cmd_service, "import": cmd_import, "db": cmd_db,
        "watch": cmd_watch, "serve": cmd_serve,
    }
    try:
        handlers[args.cmd](cfg, args)
    except KeyboardInterrupt:
        print()
        log("已退出")
    except (CovhubError, RuntimeError) as exc:
        die(str(exc))
