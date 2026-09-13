"""采集周期：快照、结算归档、断代检测。"""

from datetime import datetime
import json
import os
import shutil

from .agent import service_channel
from .collector import get_collector, collector_instances
from .diagnose import diagnose
from .jacoco import do_dump, exec_sessions, fingerprint, make_report, run_cli
from .layout import ensure_dirs, svc_dir
from .logbuf import log
from .state import load_state, record, save_state, update_state

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


def check_data_health(cfg, svc):
    """结算前体检 exec 与 class 指纹对不对得上，返回 diagnose 结果。

    **不阻断结算。** 走到 predeploy 说明服务马上要停，exec 是不可再生的 ——
    因为指纹对不上就拒绝归档，只会让这段数据既对不上、又没留下。
    所以这里只负责把话说清楚，并把结论写进 manifest，日后能追。
    """
    try:
        result = diagnose(cfg, svc)
    except Exception as exc:
        log("  ! 数据体检跳过（不影响归档）：%s" % exc)
        return None

    rate = result.get("matchRate")
    if rate is None:
        return result
    if rate < 50:
        log("  !! 指纹匹配率只有 %.1f%% —— 这一版的报告基本是废的" % rate)
        log("     %s" % result["verdict"])
        log("     exec 照常归档（不可再生），但重出报告前得先把 class 产物对上")
    elif rate < 95:
        log("  ! 指纹匹配率 %.1f%%，报告会偏低 —— %s" % (rate, result["verdict"]))
    else:
        log("  数据体检：指纹匹配 %.1f%%，正常" % rate)
    return result


def archive_cycle(cfg, svc, version, entry, out_dir, execs, reason, health=None):
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
            "matchRate": (health or {}).get("matchRate"),
            "healthVerdict": (health or {}).get("verdict"),
            "merged": os.path.basename(merged) if merged else None,
            "note": "exec 仅对本 manifest 记录的 class 产物有效（JaCoCo 按 CRC64 class id 匹配）",
        }, f, ensure_ascii=False, indent=2)

    # 体检结论跟着这一版的结算记录走，日后查「这版数字能不能信」不用翻 manifest
    if health and health.get("matchRate") is not None:
        state = load_state(cfg, svc)
        if state.get("versions"):
            state["versions"][-1]["matchRate"] = health["matchRate"]
            save_state(cfg, svc, state)

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


def detect_push_break(cfg, svc):
    """push 通道的断代检测：在线实例是不是跑着两份不同的 class。

    pull 那套（比对 SessionInfo 的启动时刻）在这里不成立 —— push 是多副本，
    副本各自重启、扩缩容都是常态，照搬过来会把每次扩容都当成一次断代。

    push 下真正会让报告出错的是**滚动发版中途**：新旧副本的数据落进同一批
    exec，对着任何一份 class 产物都只能对上一半，而 JaCoCo 不会为此报任何错。
    所以这里抓的是混版本，不是重启。

    和 pull 的另一个不同是**不自动封存**。两批数据都真实有效，只是分属两个
    版本，「到此为止」的语义不成立；而多副本下自动封存还会凭空造出一堆归档。
    这里只负责把话说清楚、记进 breaks 让看板亮起来，结算仍由 predeploy 驱动。
    """
    if get_collector() is None:
        return None
    mixed = get_collector().mixed_versions(svc["name"])
    state = load_state(cfg, svc)
    if mixed == bool(state.get("pushMixed")):
        return None                 # 状态没变。一次滚动发版会连着好几轮都成立

    entry = None
    if mixed:
        entry = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "reason": "mixed-versions",
            "instances": len(collector_instances(svc["name"])),
        }
        state.setdefault("breaks", []).append(entry)
        state["breaks"] = state["breaks"][-50:]
        log("  !! 在线实例跑着两份不同的 class —— 多半是滚动发版正在进行")
        log("     这一批 exec 跨了两个版本，对着任一份 class 产物都只能对上一半")
        log("     发版流程里补一次 predeploy，把旧版本先结算掉")
    else:
        log("  实例的 class 已经统一，混版本状态解除")
    state["pushMixed"] = mixed
    save_state(cfg, svc, state)
    return entry
def snapshot(cfg, svc, reset, kind, version=None):
    ensure_dirs(cfg, svc)
    root = svc_dir(cfg, svc)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    session_start = None
    if service_channel(svc) == "push":
        # 实例是自己连上来的，向每一个各要一次。多副本的数据落成多个 exec，
        # 出报告时一起喂给 cli report，等价于隐式 merge —— 这正是 push 通道
        # 在多副本场景比 pull 省事的地方。
        if get_collector() is None:
            raise RuntimeError("push 通道要求收集端在同一进程里，请用 serve --with-watch 启动")
        log("%s：向 %d 个在线实例取数%s"
            % (svc["name"], len(collector_instances(svc["name"])),
               "（含 --reset）" if reset else ""))
        got = get_collector().dump_service(cfg, svc, reset=reset)
        if not got:
            # 探活和取数之间实例断开就会走到这儿，属于正常情况，
            # 交给上层记日志跳过，不能是致命错误
            raise RuntimeError("%s 当前没有实例在线，取不到数据" % svc["name"])
        # push 没有「进程重启 = 计数器归零」这个信号（多副本各自重启是常态），
        # 会让报告出错的是滚动发版中途的混版本 —— 那才是这里要抓的
        detect_push_break(cfg, svc)
    else:
        # 先落到暂存位置：得先看清这份数据属于哪个进程，才知道该把它归进哪个周期。
        staging = os.path.join(root, ".incoming.exec")
        log("%s：dump%s" % (svc["name"], "（含 --reset）" if reset else ""))
        do_dump(cfg, svc, staging, reset=reset)

        if not reset:
            # --reset 自己就会把会话启动时刻往前推，只在普通采集时做断代判断，
            # 否则每次 predeploy 都会被自己误判成一次重启。
            session_start, sealed = detect_break(cfg, svc, staging)
            if sealed:
                log("  上一周期已封存，本次数据归入新周期")

        shutil.move(staging, os.path.join(root, "exec", "%s.exec" % ts))

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
