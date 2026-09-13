"""把旧的 targets.yaml（services 段）和 data/<svc>/state.json 导进数据库。

幂等：服务按 name 判断，已存在默认跳过；历史记录按内容键去重（阶段③）。
中途失败可以直接重跑。默认不动磁盘上的任何文件。
"""

from ..config import read_config_file
from ..logbuf import log
from ..schemas import ServiceSpec
from . import repo


def import_services(config_path, dry_run=False, overwrite=False):
    """返回 {name: added|updated|skipped|invalid}。"""
    raw = read_config_file(config_path)
    result = {}
    for item in raw.get("services") or []:
        name = item.get("name", "?")
        try:
            # 存原文：相对路径不展开，整个目录搬家后照样成立
            fields = ServiceSpec(**item).to_fields()
        except Exception as exc:
            log("  ! %s：配置不合法，跳过 —— %s" % (name, _brief(exc)))
            result[name] = "invalid"
            continue
        if dry_run:
            exists = name in repo.list_service_names()
            result[name] = ("updated" if overwrite else "skipped") if exists else "added"
        else:
            result[name] = repo.upsert_service(fields, overwrite=overwrite)
        log("  %s：%s" % (name, {"added": "已导入", "updated": "已更新",
                                  "skipped": "已存在，跳过（--overwrite 可覆盖）"}[result[name]]))
    return result


def _brief(exc):
    text = str(exc)
    return text.splitlines()[0] if text else type(exc).__name__
