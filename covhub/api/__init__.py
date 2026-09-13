"""FastAPI 控制面：看板静态目录 + 远程控制 API。

整套方案只需要一个服务端。被测服务所在的机器和发版节点不装 Python、不装 java、
不放配置文件，全部通过这些接口驱动 hub 干活 —— 它们只需要 curl。

两条硬约束：
- 收集端、采集线程和 HTTP 必须在**同一个进程**里（uvicorn 单 worker）；
- 令牌门禁覆盖 /api/* **和** dataDir 静态目录，只有 health 与 openapi.json 例外。
"""

from .app import create_app

__all__ = ["create_app"]
