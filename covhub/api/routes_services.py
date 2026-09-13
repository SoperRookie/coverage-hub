"""服务配置的增删改查。配置在数据库里，改完立即生效（每个请求重读）。"""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool

from .. import ops
from ..config import read_config_file
from ..locks import LOCK
from ..logbuf import capture_logs
from . import schemas
from .auth import get_cfg, require_token
from .responses import PrettyJSONResponse

router = APIRouter(prefix="/api/services", tags=["服务配置"],
                   dependencies=[Depends(require_token)])

ERR = {401: {"model": schemas.Error, "description": "令牌无效或缺失"},
       404: {"model": schemas.Error, "description": "没有这个服务"},
       409: {"model": schemas.Error, "description": "配置不合法或同名服务已存在"}}


def _one(row, status=200):
    return PrettyJSONResponse({"ok": True, "service": row}, status_code=status)


@router.get("", summary="列出全部服务", response_model=schemas.ServiceListResponse, responses=ERR)
def list_services(cfg: dict = Depends(get_cfg)):
    """返回的是入库原文：classfiles / sourcefiles 里的相对路径不展开。"""
    return PrettyJSONResponse({"ok": True, "services": ops.service_list(cfg)})


@router.get("/{name}", summary="查看一条服务", response_model=schemas.ServiceResponse, responses=ERR)
def get_service(name: str, cfg: dict = Depends(get_cfg)):
    return _one(ops.service_get(cfg, name))


@router.post("", summary="登记一条服务", status_code=201,
             response_model=schemas.ServiceResponse, responses=ERR)
def add_service(spec: schemas.ServiceSpec, cfg: dict = Depends(get_cfg)):
    """pull 通道必须给 address 和 port；拼错的键名直接拒绝。
    bindAddress / dumpRetry / version 不给就不存 —— 运行时按「键不存在」取默认。"""
    with LOCK:
        return _one(ops.service_add(cfg, spec.model_dump(exclude_unset=True)), 201)


@router.put("/{name}", summary="整份替换一条服务", response_model=schemas.ServiceResponse, responses=ERR)
def replace_service(name: str, spec: schemas.ServiceBody, cfg: dict = Depends(get_cfg)):
    """没给的可选字段会被清空。请求体里的 name 以路径为准。"""
    fields = spec.model_dump(exclude_unset=True)
    fields.pop("name", None)
    with LOCK:
        return _one(ops.service_replace(cfg, name, fields))


@router.patch("/{name}", summary="修改若干字段", response_model=schemas.ServiceResponse, responses=ERR)
def update_service(name: str, patch: schemas.ServicePatch, cfg: dict = Depends(get_cfg)):
    """只动给到的字段。改完必须仍是一条合法配置（比如不能把 pull 服务的 address 清掉）。"""
    with LOCK:
        return _one(ops.service_update(cfg, name, patch.model_dump(exclude_unset=True)))


@router.delete("/{name}", summary="删除服务配置", responses=ERR)
def remove_service(name: str, cfg: dict = Depends(get_cfg)):
    """只删配置。data/<name>/ 里的 exec 与归档**不动** —— 那是不可再生的执行轨迹。"""
    with LOCK:
        ops.service_remove(cfg, name)
    return PrettyJSONResponse({"ok": True, "service": name,
                               "note": "data/%s/ 里的采集数据未删除" % name})


import_router = APIRouter(tags=["服务配置"], dependencies=[Depends(require_token)])


@import_router.post("/api/import", summary="从旧 targets.yaml 导入服务与历史",
                    response_model=schemas.ImportResult, responses=ERR)
async def import_legacy(request: Request,
                        source: str | None = Query(None, description="hub 上的旧配置文件路径，不给则用当前配置文件"),
                        overwrite: bool = Query(False, description="同名服务已存在时覆盖"),
                        dryRun: bool = Query(False, description="只报告会做什么，不写库"),
                        cfg: dict = Depends(get_cfg)):
    """幂等，可重复调用。和 CLI 的 covhub import 是同一份实现。"""
    path = source or cfg["configPath"]
    read_config_file(path)      # 路径不对早点报错（ConfigError → 500）

    def run():
        with LOCK, capture_logs() as lines:
            result = ops.import_legacy(cfg, path, dry_run=dryRun, overwrite=overwrite)
        return lines, result

    lines, result = await run_in_threadpool(run)
    return PrettyJSONResponse({"ok": True, "log": "".join(lines), **result})
