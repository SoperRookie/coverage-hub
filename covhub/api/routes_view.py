"""前端（Vue 面板）用的只读视图接口。"""

from fastapi import APIRouter, Depends, Query

from .. import views
from . import schemas
from .auth import get_cfg, require_token
from .responses import PrettyJSONResponse

router = APIRouter(tags=["看板"], dependencies=[Depends(require_token)])

ERR = {401: {"model": schemas.Error, "description": "令牌无效或缺失"},
       404: {"model": schemas.Error, "description": "没有这个服务"}}


@router.get("/api/overview", summary="首页总览：项目 → 服务 → 指标", responses=ERR)
def overview(cfg: dict = Depends(get_cfg)):
    """每个服务一行：在线（由采集线程写入，这里不探活）、是否采集停了（stale）、
    运行时最新覆盖（总 + 新增）、单测最新覆盖（总 + 新增）、断代数。项目行只有计数，
    不算平均覆盖率。"""
    return PrettyJSONResponse({"ok": True, **views.overview(cfg)})


@router.get("/api/services/{name}/detail", summary="服务详情页的全部数据", responses=ERR)
def service_detail(name: str, cfg: dict = Depends(get_cfg)):
    """运行时：latest / history / 已结算版本 / 断代 / 在线实例 / 新增代码按文件明细；
    单测：latest / history / 新增代码按文件明细；以及报告链接。"""
    return PrettyJSONResponse({"ok": True, **views.service_detail(cfg, name)})


@router.get("/api/services/{name}/versions", summary="最近结算的版本（流水线定基线用）", responses=ERR)
def versions(name: str, cfg: dict = Depends(get_cfg)):
    """构建流水线先问 hub「上一版是谁」再算 git diff：返回最近几个已结算版本及其 diff 的 head。"""
    return PrettyJSONResponse({"ok": True, **views.versions_for_pipeline(cfg, name)})


@router.get("/api/services/{name}/source", summary="某个文件的新增代码源码视图", responses=ERR)
def incremental_source(name: str,
                       file: str = Query(..., description="新增代码明细里的文件路径"),
                       kind: str = Query("runtime", description="runtime 或 unit"),
                       context: int = Query(3, ge=0, le=20, description="新增行前后带几行上下文"),
                       cfg: dict = Depends(get_cfg)):
    """新增行标覆盖状态（covered / missed / nocode），前后带上下文。源码从服务的 sourcefiles 里找，
    找不到时只有行号与状态。"""
    if kind not in ("runtime", "unit"):
        kind = "runtime"
    return PrettyJSONResponse({"ok": True, **views.incremental_source(cfg, name, kind, file, context)})
