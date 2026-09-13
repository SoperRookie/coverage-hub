"""业务异常。

库函数里不能调 die()：它抛的是 SystemExit，会穿过 except Exception 把采集线程
静默杀死。一律抛这里的异常，由 cli.main() 翻成退出码、HTTP 层翻成状态码。
"""


class CovhubError(RuntimeError):
    """业务上失败了（目标不可达、没有数据……）。HTTP 层映射为 409。"""


class ServiceNotFound(CovhubError):
    """配置里没有这个服务。HTTP 层映射为 404。"""


class ConfigError(CovhubError):
    """配置文件读不出来或不合法。HTTP 层映射为 500。"""
