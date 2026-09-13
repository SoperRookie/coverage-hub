"""诊断 exec 与 class 产物是否对得上。"""

import json
import os

from .jacoco import class_file_ids, exec_class_ids, exec_sessions
from .layout import ensure_dirs
from .db import repo

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
        "missingSamples": [], "breaks": repo.breaks(svc["name"], 5),
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
