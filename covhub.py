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
    serve                       起 HTTP 服务托管看板
    report <service>            从已有 exec 重新生成报告

配置文件默认取当前目录的 targets.json，可用 -c 指定。
"""

import argparse
import csv
import http.server
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import time
from datetime import datetime

COUNTERS = ["INSTRUCTION", "BRANCH", "LINE", "COMPLEXITY", "METHOD"]


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def load_config(path):
    if not os.path.isfile(path):
        die("找不到配置文件 %s，先运行 covhub.py init 生成模板" % path)
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
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
#       classes/                按 reportExcludes 过滤后的 class 副本
#       state.json              历史统计，用于趋势
# --------------------------------------------------------------------------

def svc_dir(cfg, svc):
    return os.path.join(cfg["dataDir"], svc["name"])


def ensure_dirs(cfg, svc):
    root = svc_dir(cfg, svc)
    for sub in ("current", "exec", "versions", "classes"):
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
    opts = [
        "output=tcpserver",
        "address=%s" % svc.get("bindAddress", "0.0.0.0"),
        "port=%d" % svc["port"],
    ]
    if svc.get("includes"):
        opts.append("includes=" + ":".join(svc["includes"]))
    if svc.get("excludes"):
        opts.append("excludes=" + ":".join(svc["excludes"]))
    opts.append("sessionid=%s" % svc.get("version", svc["name"]))
    return "-javaagent:%s=%s" % (cfg["jacocoAgent"], ",".join(opts))


def reachable(svc, timeout=2.0):
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
        except (ValueError, OSError):
            pass
    return {"service": svc["name"], "history": [], "versions": []}


def save_state(cfg, svc, state):
    with open(state_path(cfg, svc), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


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
# 子命令
# --------------------------------------------------------------------------

def cmd_agent_opts(cfg, args):
    svc = find_service(cfg, args.service)
    print(agent_opts(cfg, svc))


def cmd_status(cfg, args):
    names = [args.service] if args.service else [s["name"] for s in cfg["services"]]
    print("%-22s %-8s %-9s %-9s %-10s %s" % ("服务", "连通", "指令%", "分支%", "版本", "最后更新"))
    print("-" * 78)
    for name in names:
        svc = find_service(cfg, name)
        state = load_state(cfg, svc)
        latest = state.get("latest") or {}
        print("%-22s %-8s %-9s %-9s %-10s %s" % (
            name,
            "ok" if reachable(svc) else "--",
            ("%.1f" % latest["instruction"]) if latest else "-",
            ("%.1f" % latest["branch"]) if latest else "-",
            latest.get("version") or svc.get("version") or "-",
            latest.get("at", "从未采集"),
        ))


def _snapshot(cfg, svc, reset, kind, version=None):
    ensure_dirs(cfg, svc)
    root = svc_dir(cfg, svc)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    exec_path = os.path.join(root, "exec", "%s.exec" % ts)

    log("%s：dump%s" % (svc["name"], "（含 --reset）" if reset else ""))
    do_dump(cfg, svc, exec_path, reset=reset)

    # 累加视图始终基于该版本周期内的全部 exec
    execs = sorted(
        os.path.join(root, "exec", f)
        for f in os.listdir(os.path.join(root, "exec")) if f.endswith(".exec")
    )
    out_dir = os.path.join(root, "current")
    summary = make_report(cfg, svc, execs, out_dir,
                          "%s (%s)" % (svc["name"], version or svc.get("version", "runtime")))
    entry = record(cfg, svc, summary, kind, version)
    log("  指令 %.1f%%（%d/%d）  分支 %.1f%%  触达类 %d/%d" % (
        summary["INSTRUCTION"]["pct"], summary["INSTRUCTION"]["covered"],
        summary["INSTRUCTION"]["total"], summary["BRANCH"]["pct"],
        summary["CLASS"]["covered"], summary["CLASS"]["total"]))
    return entry, out_dir, execs


def cmd_dump(cfg, args):
    svc = find_service(cfg, args.service)
    if not reachable(svc):
        die("连不上 %s:%d —— 确认服务在跑，且 agent 用的是 output=tcpserver"
            % (svc["address"], svc["port"]))
    _snapshot(cfg, svc, reset=False, kind="dump")
    render_dashboard(cfg)


def cmd_predeploy(cfg, args):
    """发版 / 重启前调用：结算当前版本的覆盖率并归档。

    必须在停服之前执行 —— 服务一停，agent 随之消失，数据再也拉不回来。
    """
    svc = find_service(cfg, args.service)
    version = args.version or svc.get("version") or datetime.now().strftime("%Y%m%d-%H%M%S")

    if not reachable(svc):
        msg = "连不上 %s:%d，无法结算版本 %s 的覆盖率" % (svc["address"], svc["port"], version)
        if args.allow_missing:
            log("警告：" + msg + "（--allow-missing，跳过）")
            return
        die(msg + "\n服务已经停了？那这段数据已经丢失。predeploy 必须在停服之前执行。")

    log("结算版本 %s" % version)
    entry, out_dir, execs = _snapshot(cfg, svc, reset=True, kind="predeploy", version=version)

    # 归档：报告 + 该周期全部 exec + manifest
    archive = os.path.join(svc_dir(cfg, svc), "versions", version)
    shutil.rmtree(archive, ignore_errors=True)
    shutil.copytree(out_dir, archive)
    exec_archive = os.path.join(archive, "exec")
    os.makedirs(exec_archive, exist_ok=True)
    for path in execs:
        shutil.move(path, os.path.join(exec_archive, os.path.basename(path)))
    with open(os.path.join(archive, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({
            "service": svc["name"], "version": version,
            "sealedAt": entry["at"], "summary": entry,
            "classfiles": svc["classfiles"],
            "note": "exec 仅对本 manifest 记录的 class 产物有效（JaCoCo 按 CRC64 class id 匹配）",
        }, f, ensure_ascii=False, indent=2)

    log("  已归档 → %s" % archive)
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


def cmd_watch(cfg, args):
    interval = args.interval or cfg.get("watch", {}).get("intervalSeconds", 300)
    log("守护进程启动，每 %d 秒轮询 %d 个目标（Ctrl+C 退出）"
        % (interval, len(cfg["services"])))
    while True:
        for svc in cfg["services"]:
            try:
                if not reachable(svc):
                    log("%s：离线，跳过" % svc["name"])
                    continue
                _snapshot(cfg, svc, reset=False, kind="watch")
            except Exception as exc:                      # 单个目标失败不能拖垮守护进程
                log("%s：采集失败 —— %s" % (svc["name"], exc))
        render_dashboard(cfg)
        time.sleep(interval)


def cmd_serve(cfg, args):
    port = args.port or cfg.get("serve", {}).get("port", 8900)
    root = cfg["dataDir"]
    os.makedirs(root, exist_ok=True)
    render_dashboard(cfg)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=root, **kw)

        def log_message(self, fmt, *a):
            pass

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    log("看板已启动： http://127.0.0.1:%d/  （根目录 %s）" % (port, root))
    with Server(("0.0.0.0", port), Handler) as httpd:
        httpd.serve_forever()


def cmd_init(cfg_path, _args):
    if os.path.exists(cfg_path):
        die("%s 已存在，不覆盖" % cfg_path)
    template = {
        "jacocoAgent": "./lib/jacocoagent.jar",
        "jacocoCli": "./lib/jacococli.jar",
        "dataDir": "./data",
        "serve": {"port": 8900},
        "watch": {"intervalSeconds": 300},
        "services": [{
            "name": "example-service",
            "version": "1.0.0",
            "address": "127.0.0.1",
            "port": 6300,
            "bindAddress": "0.0.0.0",
            "includes": ["com.example.*"],
            "excludes": [],
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
# 看板
# --------------------------------------------------------------------------

def render_dashboard(cfg):
    rows = []
    for svc in cfg.get("services", []):
        state = load_state(cfg, svc)
        latest = state.get("latest")
        rows.append({
            "name": svc["name"],
            "endpoint": "%s:%d" % (svc["address"], svc["port"]),
            "online": reachable(svc, timeout=1.0),
            "latest": latest,
            "history": state.get("history", [])[-40:],
            "versions": state.get("versions", [])[-10:],
            "hasReport": os.path.isfile(
                os.path.join(svc_dir(cfg, svc), "current", "html", "index.html")),
        })
    html = build_dashboard_html(rows)
    os.makedirs(cfg["dataDir"], exist_ok=True)
    with open(os.path.join(cfg["dataDir"], "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


def spark(history, key="instruction", w=132, h=30):
    """用内联 SVG 画趋势线，避免引入任何前端依赖。"""
    pts = [h["%s" % key] for h in history if key in h]
    if len(pts) < 2:
        return '<span class="nodata">数据不足</span>'
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    step = w / (len(pts) - 1)
    coords = " ".join(
        "%.1f,%.1f" % (i * step, h - 3 - (p - lo) / span * (h - 6))
        for i, p in enumerate(pts))
    return ('<svg class="spark" viewBox="0 0 %d %d" width="%d" height="%d" '
            'preserveAspectRatio="none" role="img" aria-label="覆盖率趋势">'
            '<polyline points="%s" fill="none" stroke="currentColor" '
            'stroke-width="1.5" stroke-linejoin="round"/></svg>' % (w, h, w, h, coords))


def build_dashboard_html(rows):
    cards = []
    for r in rows:
        latest = r["latest"]
        if latest:
            inst, br = latest["instruction"], latest["branch"]
            detail = ('<div class="figs">'
                      '<div class="fig"><span class="fv">%.1f<i>%%</i></span><span class="fl">指令</span></div>'
                      '<div class="fig"><span class="fv">%.1f<i>%%</i></span><span class="fl">分支</span></div>'
                      '<div class="fig"><span class="fv">%d<i>/%d</i></span><span class="fl">触达类</span></div>'
                      '</div>'
                      '<div class="bar"><i style="width:%.2f%%"></i></div>'
                      '<div class="sub">%s · 版本 %s · %s</div>'
                      % (inst, br, latest["classesHit"], latest["classesTotal"], inst,
                         latest["at"].replace("T", " "), latest.get("version") or "-",
                         "{:,} / {:,} 条指令".format(latest["covered"], latest["total"])))
        else:
            detail = '<div class="empty">尚未采集到数据</div>'

        links = []
        if r["hasReport"]:
            links.append('<a href="%s/current/html/index.html">打开报告</a>' % r["name"])
            links.append('<a href="%s/current/jacoco.xml">jacoco.xml</a>' % r["name"])
        if r["versions"]:
            vs = " ".join('<a href="%s/versions/%s/html/index.html">%s</a>'
                          % (r["name"], v["version"], v["version"])
                          for v in reversed(r["versions"]))
            links.append('<span class="vers">已结算版本：%s</span>' % vs)

        cards.append(
            '<article class="card">'
            '<header><h2>%s</h2><span class="dot %s"></span>'
            '<span class="ep">%s</span></header>'
            '%s'
            '<div class="trend">%s</div>'
            '<div class="links">%s</div>'
            '</article>'
            % (r["name"], "on" if r["online"] else "off", r["endpoint"],
               detail, spark(r["history"]), " ".join(links)))

    # 模板内嵌 CSS 含大量字面 % （50%、100%），不能用 % 格式化，改用占位符替换
    values = {
        "{{GENERATED}}": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "{{COUNT}}": str(len(rows)),
        "{{ONLINE}}": str(sum(1 for r in rows if r["online"])),
        "{{CARDS}}": "\n".join(cards) or '<p class="empty">配置里还没有任何服务。</p>',
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
<title>运行期覆盖率看板</title>
<style>
  :root {
    --bg:#fbfcfb; --surface:#eef3f0; --fg:#14201a; --muted:#5a6b62; --faint:#84968d;
    --border:#d7e0db; --accent:#0d7052; --covered:#3f9c53; --missed:#c0392b; --off:#b3bfb8;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg:#0d1411; --surface:#16201b; --fg:#dfe9e4; --muted:#98aaa1; --faint:#7b8d84;
      --border:#26332c; --accent:#4ec59d; --covered:#5cb872; --missed:#e0685a; --off:#3d4a43;
    }
  }
  html { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
    font-family:"IBM Plex Sans","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
    font-size:15px; line-height:1.6; }
  .wrap { max-width:1080px; margin:0 auto; padding:40px 24px 80px;
    display:flex; flex-direction:column; gap:28px; }
  header.top { display:flex; flex-direction:column; gap:6px; }
  h1 { font-size:26px; margin:0; letter-spacing:-.01em; }
  .meta { color:var(--muted); font-size:13.5px;
    font-family:ui-monospace,"SFMono-Regular",Consolas,monospace; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:16px; }
  .card { border:1px solid var(--border); border-radius:4px; background:var(--bg);
    padding:18px; display:flex; flex-direction:column; gap:14px; }
  .card header { display:flex; align-items:center; gap:9px; }
  .card h2 { font-size:16.5px; margin:0; font-weight:600; }
  .dot { width:8px; height:8px; border-radius:50%; background:var(--off); flex:none; }
  .dot.on { background:var(--covered); }
  .ep { margin-left:auto; font-size:12px; color:var(--faint);
    font-family:ui-monospace,Consolas,monospace; }
  .figs { display:flex; gap:22px; }
  .fig { display:flex; flex-direction:column; }
  .fv { font-size:23px; font-weight:600; font-variant-numeric:tabular-nums;
    font-family:ui-monospace,Consolas,monospace; }
  .fv i { font-style:normal; font-size:13px; color:var(--faint); font-weight:400; }
  .fl { font-size:11.5px; color:var(--faint); letter-spacing:.06em; }
  .bar { height:7px; background:var(--missed); border-radius:1px; overflow:hidden; }
  .bar i { display:block; height:100%; background:var(--covered); }
  .sub { font-size:12.5px; color:var(--muted);
    font-family:ui-monospace,Consolas,monospace; }
  .trend { color:var(--accent); min-height:30px; }
  .spark { display:block; width:100%; height:30px; }
  .nodata, .empty { color:var(--faint); font-size:13px; }
  .links { display:flex; flex-wrap:wrap; gap:12px; font-size:13px;
    border-top:1px solid var(--border); padding-top:12px; margin-top:auto; }
  .links a { color:var(--accent); text-decoration:none; }
  .links a:hover { text-decoration:underline; }
  .links a:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
  .vers { color:var(--faint); font-size:12px; width:100%; }
  .vers a { margin-left:6px; }
  footer { color:var(--faint); font-size:12.5px; border-top:1px solid var(--border);
    padding-top:16px; }
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <h1>运行期覆盖率看板</h1>
    <div class="meta">{{COUNT}} 个目标 · {{ONLINE}} 个在线 · 生成于 {{GENERATED}} · 每 60 秒自动刷新</div>
  </header>
  <div class="grid">
{{CARDS}}
  </div>
  <footer>由 covhub 生成。报告数据来自各服务 JaCoCo agent 的 tcpserver 端口，覆盖率随操作实时累加；
  「已结算版本」是发版前经 predeploy 归档的快照，与当时的 class 产物一一对应。</footer>
</div>
</body>
</html>
"""


# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="covhub", description="通用 JaCoCo 运行期覆盖率采集与看板")
    parser.add_argument("-c", "--config", default="targets.json", help="配置文件路径")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="生成配置模板")

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

    p = sub.add_parser("watch", help="守护进程：定时轮询全部目标")
    p.add_argument("--interval", type=int, help="间隔秒数")

    p = sub.add_parser("serve", help="起 HTTP 服务托管看板")
    p.add_argument("--port", type=int)

    args = parser.parse_args()

    if args.cmd == "init":
        return cmd_init(args.config, args)

    cfg = load_config(args.config)
    for key in ("jacocoCli", "jacocoAgent"):
        if not os.path.isfile(cfg.get(key, "")):
            die("配置项 %s 指向的文件不存在：%s" % (key, cfg.get(key)))
    os.makedirs(cfg["dataDir"], exist_ok=True)

    handlers = {
        "agent-opts": cmd_agent_opts, "status": cmd_status, "dump": cmd_dump,
        "predeploy": cmd_predeploy, "report": cmd_report, "watch": cmd_watch,
        "serve": cmd_serve,
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
