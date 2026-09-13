"""进程内写锁。

采集轮询和控制 API 在同一个进程里，写操作（快照、结算、上传产物）全部在这把锁
里排队 —— 结算和轮询不会打架。别绕过它。
"""

import threading

LOCK = threading.RLock()
