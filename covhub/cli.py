"""命令行入口。

这里只做三件事：解析参数、调 ops、打印。die() 只允许在这个模块出现 ——
库函数抛 CovhubError，由 main() 统一翻成退出码。
"""

import argparse
import json
import os
import sys

from . import __version__, ops
from .config import (CONFIG_CANDIDATES, CONFIG_TEMPLATE_YAML, config_format, load_config,
                     resolve_config_path)
from .errors import CovhubError
from .httpd import cmd_serve
from .logbuf import log
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
    try:
        cfg = load_config(args.config)
    except CovhubError as exc:
        die(str(exc))
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
    except (CovhubError, RuntimeError) as exc:
        die(str(exc))

