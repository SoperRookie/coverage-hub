"""把演示用的 20 个微服务真的跑起来：每个服务一个小 JVM，挂 JaCoCo agent，hub 能采到真实数据。

    python tools/demo_services.py start     # 生成 + 编译 + 启动 20 个 JVM，更新 hub 配置，推 diff，采一次
    python tools/demo_services.py stop      # 停掉它们
    python tools/demo_services.py status    # 谁在跑

要 JDK（javac / java 在 PATH 上）。每个 JVM 限到 48 MB 堆，20 个约 1 GB 内存。
工作目录默认在系统临时目录 covhub-demo/<服务>/：src/main/java（源码）、classes（编译产物）、
dump（agent 的 classdumpdir）、jvm.log、pid。

跑起来的进程随机调自己的方法，覆盖率随时间慢慢涨；每个类有几个几乎不会被调到的方法，
所以永远到不了 100%。「本版本新增代码」是真的：当前版本比上一版多了几个方法，diff 用
difflib 现算后推给 hub，看板上的新增代码源码视图看到的就是这些真实源码。
"""
import argparse
import difflib
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
from seed_demo import MALL, PAY  # noqa: E402  (同目录)

from covhub import build  # noqa: E402
from covhub.config import load_config, resolve_config_path  # noqa: E402
from covhub.db import repo  # noqa: E402
from covhub.runtime import prepare_database  # noqa: E402
from covhub.schemas import ServicePatch  # noqa: E402

ROOT = os.path.join(tempfile.gettempdir(), "covhub-demo")
AGENT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib", "jacocoagent.jar")
METHOD_NAMES = ["create", "query", "update", "cancel", "confirm", "pay", "refund", "sync", "lock", "release",
                "settle", "notify", "route", "verify", "audit", "retry", "export", "validate"]


def class_source(pkg, cls, methods):
    """一个可编译的类：每个方法带分支；名字以 Rarely 结尾的方法 Main 几乎不会调。"""
    out = ["package %s;" % pkg, "", "import java.util.concurrent.atomic.AtomicLong;", "",
           "/** 演示服务里的一个组件（自动生成）。 */", "public class %s {" % cls, "",
           "    private final AtomicLong calls = new AtomicLong();", "    private long lastId = -1;", ""]
    for m, added in methods:
        out.append("    /** %s%s */" % (m, "（本版本新增）" if added else ""))
        out.append("    public long %s(long id, int amount) {" % m)
        out.append("        calls.incrementAndGet();")
        out.append("        if (amount < 0) {")
        out.append("            throw new IllegalArgumentException(\"amount\");")
        out.append("        }")
        out.append("        long result = id * 31 + amount;")
        out.append("        if (amount % 3 == 0) {")
        out.append("            result += %d;" % (len(m) * 7))
        out.append("        } else if (amount % 3 == 1) {")
        out.append("            result -= %d;" % len(m))
        out.append("        }")
        out.append("        if (id == lastId) {")
        out.append("            result = -result;")
        out.append("        }")
        out.append("        lastId = id;")
        out.append("        return result;")
        out.append("    }")
        out.append("")
    out.append("    public long calls() {")
    out.append("        return calls.get();")
    out.append("    }")
    out.append("}")
    return "\n".join(out) + "\n"


def main_source(pkg, classes):
    """Main：随机挑组件、随机挑方法调用；Rarely 方法 2% 概率。"""
    out = ["package %s;" % pkg, "", "import java.util.Random;", "", "public class Main {",
           "    public static void main(String[] args) throws Exception {",
           "        Random rnd = new Random();", "        long tick = 0;"]
    for i, (cls, _) in enumerate(classes):
        out.append("        %s c%d = new %s();" % (cls, i, cls))
    out.append("        System.out.println(\"demo service up: %s\");" % pkg)
    out.append("        while (true) {")
    out.append("            int amount = rnd.nextInt(9);")
    out.append("            long id = rnd.nextInt(50);")
    out.append("            switch (rnd.nextInt(%d)) {" % len(classes))
    for i, (cls, methods) in enumerate(classes):
        out.append("                case %d: {" % i)
        out.append("                    int pick = rnd.nextInt(100);")
        normal = [m for m, _ in methods if not m.endswith("Rarely")]
        rare = [m for m, _ in methods if m.endswith("Rarely")]
        step = max(1, (100 - 2 * len(rare)) // max(1, len(normal)))
        bound = 0
        for m in normal:
            bound += step
            out.append("                    if (pick < %d) { c%d.%s(id, amount); break; }" % (bound, i, m))
        for m in rare:
            bound += 2
            out.append("                    if (pick < %d) { c%d.%s(id, amount); break; }" % (bound, i, m))
        out.append("                    break;")
        out.append("                }")
    out.append("                default: break;")
    out.append("            }")
    out.append("            if (++tick % 200 == 0) { System.out.println(\"tick \" + tick); }")
    out.append("            Thread.sleep(300 + rnd.nextInt(900));")
    out.append("        }")
    out.append("    }")
    out.append("}")
    return "\n".join(out) + "\n"


def generate(spec, rnd):
    """生成一个服务的源码（当前版本）与上一版的源码文本，返回 (dir, {rel: cur_text}, {rel: prev_text})。"""
    name, _, pkg, class_names = spec
    d = os.path.join(ROOT, name)
    src = os.path.join(d, "src", "main", "java", *pkg.split("."))
    os.makedirs(src, exist_ok=True)
    cur, prev = {}, {}
    classes = []
    changed = rnd.sample(class_names, min(len(class_names), rnd.randint(2, 3)))
    for cls in class_names:
        names = rnd.sample(METHOD_NAMES, rnd.randint(3, 6))
        methods = [(m, False) for m in names] + [(rnd.choice(METHOD_NAMES) + "Rarely", False)]
        if cls in changed:
            methods += [(n + "V2", True) for n in rnd.sample([n for n in METHOD_NAMES if n not in names], rnd.randint(1, 2))]
        rel = "src/main/java/%s/%s.java" % (pkg.replace(".", "/"), cls)
        cur[rel] = class_source(pkg, cls, methods)
        prev[rel] = class_source(pkg, cls, [(m, a) for m, a in methods if not a])
        classes.append((cls, methods))
        with open(os.path.join(src, cls + ".java"), "w", encoding="utf-8") as f:
            f.write(cur[rel])
    with open(os.path.join(src, "Main.java"), "w", encoding="utf-8") as f:
        f.write(main_source(pkg, classes))
    return d, cur, prev


def unified(prev, cur):
    out = []
    for rel in sorted(cur):
        if prev.get(rel) == cur[rel]:
            continue
        a = prev.get(rel, "").splitlines(keepends=True)
        b = cur[rel].splitlines(keepends=True)
        out.append("diff --git a/%s b/%s\n" % (rel, rel))
        out.extend(difflib.unified_diff(a, b, fromfile="a/" + rel, tofile="b/" + rel, n=0))
    return "".join(out)


def compile_service(d):
    src_root = os.path.join(d, "src", "main", "java")
    classes = os.path.join(d, "classes")
    shutil.rmtree(classes, ignore_errors=True)
    os.makedirs(classes)
    files = [os.path.join(dp, f) for dp, _, fs in os.walk(src_root) for f in fs if f.endswith(".java")]
    subprocess.run(["javac", "-nowarn", "-d", classes, "-encoding", "UTF-8"] + files, check=True)
    return classes


def pid_file(name):
    return os.path.join(ROOT, name, "pid")


def alive(pid):
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def running_pid(name):
    try:
        with open(pid_file(name)) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    return pid if alive(pid) else None


def start_jvm(name, spec, svc, classes):
    _, _, pkg, _ = spec
    d = os.path.join(ROOT, name)
    dump = os.path.join(d, "dump")
    os.makedirs(dump, exist_ok=True)
    agent = ("-javaagent:%s=output=tcpserver,address=127.0.0.1,port=%d,includes=%s.*,classdumpdir=%s,sessionid=%s"
             % (AGENT, svc["port"], pkg, dump.replace("\\", "/"), svc.get("version") or "dev"))
    cmd = ["java", agent, "-Xmx48m", "-Xss512k", "-XX:+UseSerialGC", "-XX:TieredStopAtLevel=1",
           "-cp", classes, pkg + ".Main"]
    log = open(os.path.join(d, "jvm.log"), "ab")
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kw["start_new_session"] = True
    p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=d, **kw)
    with open(pid_file(name), "w") as f:
        f.write(str(p.pid))
    return p.pid


def hub_post(base, token, path):
    req = urllib.request.Request(base + path, method="POST", headers={"X-Covhub-Token": token or ""})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)


def cmd_start(args, cfg):
    rnd = random.Random(args.seed)
    specs = [(s, "mall") for s in MALL[3]] + [(s, "payment") for s in PAY[3]]
    started = []
    for spec, _ in specs:
        name = spec[0]
        if running_pid(name):
            print("  %-20s 已在跑" % name)
            continue
        try:
            svc = repo.get_service(name, cfg)
        except Exception:
            print("  %-20s hub 里没登记（先跑 seed_demo.py）" % name)
            continue
        d, cur, prev = generate(spec, rnd)
        classes = compile_service(d)
        # hub 侧：class 与源码指到真实目录，出报告才有源码行
        repo.update_service(name, ServicePatch(classfiles=[classes], sourcefiles=[os.path.join(d, "src", "main", "java")],
                                               classDumpDir=os.path.join(d, "dump")).to_fields())
        svc = repo.get_service(name, cfg)
        # 当前版本的 diff：真实源码 prev → cur
        build.store_diff(cfg, svc, svc["version"], "v-prev", "v-" + svc["version"], unified(prev, cur))
        pid = start_jvm(name, spec, svc, classes)
        started.append(name)
        print("  %-20s 端口 %d  pid %d" % (name, svc["port"], pid))
    if not started:
        return
    print("等 JVM 起来…")
    time.sleep(4)
    if args.hub:
        print("让 hub 各采一次：")
        token = (cfg.get("serve") or {}).get("token") or os.environ.get("COVHUB_TOKEN", "")
        for name in started:
            r = hub_post(args.hub, token, "/api/dump?service=" + name)
            ok = r.get("ok")
            latest = r.get("latest") or {}
            print("  %-20s %s %s" % (name, "ok" if ok else "失败", ("指令 %.1f%%" % latest["instruction"]) if ok and latest else r.get("log", "")[-120:].strip()))


def cmd_stop(args, cfg):
    for spec in MALL[3] + PAY[3]:
        name = spec[0]
        pid = running_pid(name)
        if not pid:
            continue
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
        print("  %-20s 已停（pid %d）" % (name, pid))
        try:
            os.unlink(pid_file(name))
        except OSError:
            pass


def cmd_status(args, cfg):
    for spec in MALL[3] + PAY[3]:
        pid = running_pid(spec[0])
        print("  %-20s %s" % (spec[0], ("在跑 pid %d" % pid) if pid else "没跑"))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=["start", "stop", "status"])
    ap.add_argument("-c", "--config", help="hub 配置文件")
    ap.add_argument("--hub", default="http://127.0.0.1:8900", help="启动后让这个 hub 各采一次；传空串跳过")
    ap.add_argument("--seed", type=int, default=2026)
    args = ap.parse_args()
    cfg = load_config(resolve_config_path(args.config))
    prepare_database(cfg)
    os.makedirs(ROOT, exist_ok=True)
    {"start": cmd_start, "stop": cmd_stop, "status": cmd_status}[args.action](args, cfg)


if __name__ == "__main__":
    main()
