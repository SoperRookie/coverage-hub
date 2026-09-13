"""定时轮询。"""

import threading

from .agent import reachable
from .runtime import load_runtime
from .cycle import snapshot
from .dashboard import render_dashboard
from .locks import LOCK
from .logbuf import log

def watch_once(cfg):
    for svc in cfg["services"]:
        try:
            if not reachable(svc):
                log("%s：离线，跳过" % svc["name"])
                continue
            with LOCK:
                snapshot(cfg, svc, reset=False, kind="watch")
        except (Exception, SystemExit) as exc:
            # 也接住 SystemExit：die() 抛的就是它，穿过 except Exception 会把
            # 采集线程静默杀死 —— 守护进程无声停摆是最坏的失败模式。
            # KeyboardInterrupt 不在此列，Ctrl+C 仍然能正常退出。
            log("%s：采集失败 —— %s" % (svc["name"], exc))
    render_dashboard(cfg)


def watch_loop(cfg_path, interval, stop=None):
    """每轮重新加载配置 —— retarget 换了 classfiles 之后不必重启采集进程。

    stop 是一个 threading.Event：serve 退出时 set 一下，循环立刻收场，
    不用等完一整个间隔。
    """
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            watch_once(load_runtime(cfg_path))
        except (Exception, SystemExit) as exc:
            log("轮询失败 —— %s" % exc)
        if stop.wait(interval):
            return
