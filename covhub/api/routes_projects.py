"""项目（服务的分组）的增删改查。"""

from fastapi import APIRouter, Depends

from .. import ops
from ..locks import LOCK
from . import schemas
from .auth import get_cfg, require_token
from .responses import PrettyJSONResponse

router = APIRouter(prefix="/api/projects", tags=["项目"], dependencies=[Depends(require_token)])

ERR = {401: {"model": schemas.Error, "description": "令牌无效或缺失"},
       404: {"model": schemas.Error, "description": "没有这个项目"},
       409: {"model": schemas.Error, "description": "不合法或同名项目已存在"}}


def _one(row, status=200):
    return PrettyJSONResponse({"ok": True, "project": row}, status_code=status)


@router.get("", summary="列出全部项目", response_model=schemas.ProjectListResponse, responses=ERR)
def list_projects(cfg: dict = Depends(get_cfg)):
    return PrettyJSONResponse({"ok": True, "projects": ops.project_list(cfg)})


@router.get("/{name}", summary="查看一个项目", response_model=schemas.ProjectResponse, responses=ERR)
def get_project(name: str, cfg: dict = Depends(get_cfg)):
    return _one(ops.project_get(cfg, name))


@router.post("", summary="创建项目", status_code=201,
             response_model=schemas.ProjectResponse, responses=ERR)
def add_project(spec: schemas.ProjectSpec, cfg: dict = Depends(get_cfg)):
    with LOCK:
        return _one(ops.project_add(cfg, spec.model_dump()), 201)


@router.patch("/{name}", summary="修改项目的标题 / 描述",
              response_model=schemas.ProjectResponse, responses=ERR)
def update_project(name: str, patch: schemas.ProjectPatch, cfg: dict = Depends(get_cfg)):
    with LOCK:
        return _one(ops.project_update(cfg, name, patch.model_dump(exclude_unset=True)))


@router.delete("/{name}", summary="删除项目", responses=ERR)
def remove_project(name: str, cfg: dict = Depends(get_cfg)):
    """只删分组。项目下的服务变成未分组，配置与采集数据都不动。"""
    with LOCK:
        ops.project_remove(cfg, name)
    return PrettyJSONResponse({"ok": True, "project": name, "note": "项目下的服务已改为未分组"})
