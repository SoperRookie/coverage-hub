"""把「hub 配置文件 + 数据库里的服务列表」合成一份 cfg dict。

所有入口（CLI、HTTP 每个请求、每轮轮询、收集端认领连接）都走这里重读 ——
「配置每次重读」是硬约束，retarget / service update 之后不必重启任何东西。
"""

from . import config
from .config import find_service
from .db import engine, migrate, repo
from .logbuf import log

_schema_checked = set()

__all__ = ["find_service", "load_runtime", "prepare_database"]


def prepare_database(cfg):
    """建引擎，并保证表结构是最新的（每个进程、每个 URL 只检查一次）。"""
    url = config.resolve_database_url(cfg)
    engine.configure(url)
    if url not in _schema_checked:
        if config.database_auto_upgrade(cfg):
            migrate.upgrade(url)
        else:
            migrate.ensure_head(url)
        _schema_checked.add(url)
    return url


def load_runtime(cfg_path):
    cfg = config.load_config(cfg_path)
    prepare_database(cfg)
    cfg["services"] = repo.list_services(cfg)
    if not cfg["services"] and config.read_config_file(cfg_path).get("services"):
        log("! 数据库里还没有任何服务，而 %s 里有 —— 先跑 covhub import 导入" % cfg_path)
    return cfg
