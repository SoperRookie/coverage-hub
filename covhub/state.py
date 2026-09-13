"""state.json 读写（阶段③改为数据库后删除）。"""

from datetime import datetime
import json
import os

from .layout import svc_dir
from .logbuf import log

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
        except (ValueError, OSError) as exc:
            # 读不出来 = 历史清零 + 会话基线丢失，而采集会照常跑下去、断代检测
            # 从此哑火。静默吞掉是最坏的处理方式，至少得在日志里留下痕迹。
            log("! %s 的 state.json 读取失败，按空状态继续：%s" % (svc["name"], exc))
    return {"service": svc["name"], "history": [], "versions": []}


def save_state(cfg, svc, state):
    """先写临时文件再 os.replace —— state.json 不能有"写了一半"的中间态。

    它一个文件装着 history、versions、breaks 和 sessionStart，直接原地覆写时
    只要在中途断电或被 kill，就会留下半个 JSON；而 load_state 拿不到内容只会
    退回空状态，一声不吭地把历史和断代基线一起丢掉。配置回写早就是这个待遇了
    （见 yaml_update_service），这里跟上。
    """
    path = state_path(cfg, svc)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


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
