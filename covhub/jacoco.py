"""jacococli 的调用与输出解析、class 过滤。"""

import csv
import hashlib
import os
import re
import shutil
import subprocess
import tempfile

from .layout import svc_dir
from .logbuf import log

COUNTERS = ["INSTRUCTION", "BRANCH", "LINE", "COMPLEXITY", "METHOD"]

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
# 一个版本周期里每 5 分钟一份快照，跑上两天就是五六百个 exec，全部塞进一条命令行
# 在 Windows 上会撞 CreateProcess 的 32767 字符上限（WinError 206）。Linux 的上限
# 高得多，但同样有限，所以统一按这个阈值分批，不区分平台。
MAX_CMDLINE = 30000


def run_cli(cfg, args, quiet=True):
    cmd = ["java", "-jar", cfg["jacocoCli"]] + args
    if quiet:
        cmd.append("--quiet")
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip()[:600])
    return proc.stdout


def _cmdline_len(cfg, args):
    return len(subprocess.list2cmdline(["java", "-jar", cfg["jacocoCli"]] + list(args) + ["--quiet"]))


def _batches(cfg, head, files, tail=()):
    """把 files 切成若干批，保证 head + 批 + tail 拼成的命令行不超长。"""
    files = list(files)
    if not files:
        return
    batch = []
    for path in files:
        if batch and _cmdline_len(cfg, list(head) + batch + [path] + list(tail)) > MAX_CMDLINE:
            yield batch
            batch = []
        batch.append(path)
    yield batch


def merge_execs(cfg, execfiles, dest):
    """把多个 exec 合并成 dest。文件多到一条命令放不下时分批滚动合并。"""
    execfiles = list(execfiles)
    tmp = dest + ".part"
    first = True
    # 每批都带上前一轮的结果：merge 的输入和输出不能是同一个文件，所以经 .part 中转
    try:
        for batch in _batches(cfg, ["merge", dest], execfiles, ["--destfile", tmp]):
            inputs = batch if first else [dest] + batch
            run_cli(cfg, ["merge"] + inputs + ["--destfile", tmp])
            os.replace(tmp, dest)
            first = False
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return dest


def _execinfo(cfg, execfiles):
    return "".join(run_cli(cfg, ["execinfo"] + batch, quiet=False)
                   for batch in _batches(cfg, ["execinfo"], execfiles))
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
    out = _execinfo(cfg, execfiles)
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
    out = _execinfo(cfg, execfiles)
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
    """生成 HTML + XML + CSV。XML 是看板与新增覆盖计算读的那份。"""
    classfiles = prepare_classfiles(cfg, svc)
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)

    tail = []
    for path in classfiles:
        tail += ["--classfiles", path]
    for path in svc.get("sourcefiles", []):
        tail += ["--sourcefiles", path]
    tail += [
        "--html", os.path.join(out_dir, "html"),
        "--xml", os.path.join(out_dir, "jacoco.xml"),
        "--csv", os.path.join(out_dir, "jacoco.csv"),
        "--name", name,
        "--encoding", svc.get("sourceEncoding", "UTF-8"),
    ]
    execfiles = list(execfiles)
    merged = None
    try:
        # report 内部就是把所有 exec 先加载合并再统计，所以先 merge 成一个临时文件
        # 再出报告结果完全一样；只在命令行放不下时才这么绕一圈
        if len(execfiles) > 1 and _cmdline_len(cfg, ["report"] + execfiles + tail) > MAX_CMDLINE:
            fd, merged = tempfile.mkstemp(suffix=".exec", prefix="covhub-report-")
            os.close(fd)
            log("  exec 有 %d 份，先合并再出报告" % len(execfiles))
            merge_execs(cfg, execfiles, merged)
            execfiles = [merged]
        run_cli(cfg, ["report"] + execfiles + tail)
    finally:
        if merged:
            try:
                os.remove(merged)
            except OSError:
                pass
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
