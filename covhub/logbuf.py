"""带时间戳的日志，以及按请求收集日志的汇。

HTTP 写接口要把这次执行打印的日志塞进响应体。以前靠 redirect_stdout，那是
进程级的替换 —— API 调用期间采集线程的日志会串进别人的响应里。这里改成
ContextVar：请求线程池会拷贝上下文，采集线程和收集端线程的上下文是空的，
各自的日志互不串门。
"""

import contextlib
import contextvars
from datetime import datetime

_sink = contextvars.ContextVar("covhub_log", default=None)


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    sink = _sink.get()
    if sink is not None:
        sink.append(line + "\n")


@contextlib.contextmanager
def capture_logs():
    """在这个 with 块（以及从它派生的 contextvars 上下文）里打的日志都收进列表。"""
    lines = []
    token = _sink.set(lines)
    try:
        yield lines
    finally:
        _sink.reset(token)
