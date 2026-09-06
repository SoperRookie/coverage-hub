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

配置文件默认取当前目录的 targets.json，可用 -c 指定。
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
import shutil
import socket
import socketserver
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import zipfile
from datetime import datetime

__version__ = "1.1.0"

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
        except (ValueError, OSError):
            pass
    return {"service": svc["name"], "history": [], "versions": []}


def save_state(cfg, svc, state):
    with open(state_path(cfg, svc), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


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


def archive_cycle(cfg, svc, version, entry, out_dir, execs, reason):
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
            "merged": os.path.basename(merged) if merged else None,
            "note": "exec 仅对本 manifest 记录的 class 产物有效（JaCoCo 按 CRC64 class id 匹配）",
        }, f, ensure_ascii=False, indent=2)

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
            print("            %s  %s → %s  已结算为 %s"
                  % (b["at"], b["from"], b["to"], b["sealedAs"]))


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
    return {
        "name": svc["name"],
        "endpoint": "%s:%d" % (svc["address"], svc["port"]),
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
        print("%-22s %-8s %-9s %-9s %-10s %s" % (
            row["name"],
            "ok" if row["online"] else "--",
            ("%.1f" % latest["instruction"]) if latest else "-",
            ("%.1f" % latest["branch"]) if latest else "-",
            row["version"] or "-",
            latest.get("at", "从未采集"),
        ))


def _snapshot(cfg, svc, reset, kind, version=None):
    ensure_dirs(cfg, svc)
    root = svc_dir(cfg, svc)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    # 先落到暂存位置：得先看清这份数据属于哪个进程，才知道该把它归进哪个周期。
    staging = os.path.join(root, ".incoming.exec")
    log("%s：dump%s" % (svc["name"], "（含 --reset）" if reset else ""))
    do_dump(cfg, svc, staging, reset=reset)

    session_start = None
    if not reset:
        # --reset 自己就会把会话启动时刻往前推，只在普通采集时做断代判断，
        # 否则每次 predeploy 都会被自己误判成一次重启。
        session_start, sealed = detect_break(cfg, svc, staging)
        if sealed:
            log("  上一周期已封存，本次数据归入新周期")

    exec_path = os.path.join(root, "exec", "%s.exec" % ts)
    shutil.move(staging, exec_path)

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
    archive = archive_cycle(cfg, svc, version, entry, out_dir, execs, "predeploy")
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


def cmd_retarget(cfg, args):
    """发版后把配置指向新版本的 class 产物。

    JaCoCo 按 CRC64 class id 匹配数据，class 产物不跟着版本换，新周期采到的 exec
    就和旧 class 对不上，报告全是"未覆盖"。这一步是发版流水线里最容易漏的。

    直接改配置文件原文（而不是回写 load_config 解析后的结果），避免把相对路径
    固化成绝对路径 —— 整个目录要能原样搬到别的机器上。
    """
    path = args.config
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    hit = next((s for s in raw.get("services", []) if s["name"] == args.service), None)
    if hit is None:
        die("配置里没有名为 %r 的服务" % args.service)
    if args.version:
        hit["version"] = args.version
    if args.classfiles:
        hit["classfiles"] = list(args.classfiles)
    if args.sourcefiles:
        hit["sourcefiles"] = list(args.sourcefiles)

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    log("%s -> version=%s classfiles=%s"
        % (args.service, hit.get("version"), hit.get("classfiles")))


# 采集、结算、改配置都会写 data/ 与 targets.json，单进程内一律串行，
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
        except Exception as exc:                      # 单个目标失败不能拖垮守护进程
            log("%s：采集失败 —— %s" % (svc["name"], exc))
    render_dashboard(cfg)


def watch_loop(cfg_path, interval):
    """每轮重新加载配置 —— retarget 换了 classfiles 之后不必重启采集进程。"""
    while True:
        try:
            watch_once(load_config(cfg_path))
        except Exception as exc:
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
# 不放 targets.json，全部通过这些接口驱动 hub 干活 —— 它们只需要 curl。
#
#   GET  /api/health                            存活探测，不需要令牌
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


def _token(cfg):
    return os.environ.get("COVHUB_TOKEN") or (cfg.get("serve") or {}).get("token") or ""


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
            return super().do_GET()

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

        def _authorized(self, current, params):
            expected = _token(current)
            if not expected or self._route() == "/api/health":
                return True
            given = self.headers.get("X-Covhub-Token") or params.get("token") or ""
            return given == expected

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
            if route != "/api/health":
                log("%s %s -> %d" % (method, self.path, code))

            # 纯文本模式，方便 shell 里直接 $(curl ...) 取参数串
            if code == 200 and params.get("format") == "text" and "agentOpts" in body:
                return self._send(200, (body["agentOpts"] + "\n").encode("utf-8"),
                                  "text/plain; charset=utf-8")
            self._send(code, body)

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    if getattr(args, "with_watch", False):
        interval = args.interval or cfg.get("watch", {}).get("intervalSeconds", 300)
        threading.Thread(target=watch_loop, args=(cfg_path, interval), daemon=True).start()
        log("采集线程已启动，每 %d 秒轮询一次" % interval)

    log("covhub %s 已启动： http://127.0.0.1:%d/  （根目录 %s）" % (__version__, port, root))
    log("控制 API： http://127.0.0.1:%d/api/health%s"
        % (port, "" if _token(cfg) else "    [未设置 serve.token，任何人都能调写接口]"))
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
    parser.add_argument("-V", "--version", action="version",
                        version="covhub %s" % __version__)
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

    p = sub.add_parser("diagnose", help="诊断 exec 与 class 产物是否对得上")
    p.add_argument("service")
    p.add_argument("--version", help="诊断某个已归档版本，缺省诊断当前周期")

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
        return cmd_init(args.config, args)

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
