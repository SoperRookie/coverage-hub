"""构建期送进来的东西：单测 jacoco.xml 与 git diff。

两个接口的正文都是原始文件，照 upload-classes 的方式流式落盘：不挂 merged_params、
不查 Content-Type —— curl --data-binary 默认发的是 x-www-form-urlencoded，按表单
解析会把 XML / diff 吃掉。锁在线程池里拿。
"""

import os
import tempfile

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool

from .. import ops
from ..config import find_service
from ..locks import LOCK
from ..logbuf import capture_logs
from . import schemas
from .auth import get_cfg, require_token
from .params import as_list
from .responses import PrettyJSONResponse, error

router = APIRouter(tags=["构建期"], dependencies=[Depends(require_token)])

ERR = {400: {"model": schemas.Error, "description": "正文为空、不是 JaCoCo XML / git diff、或版本串不能作目录名"},
       401: {"model": schemas.Error, "description": "令牌无效或缺失"},
       404: {"model": schemas.Error, "description": "没有这个服务"}}

RAW_BODY = {"required": True, "content": {
    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
    "text/plain": {"schema": {"type": "string"}},
    "application/xml": {"schema": {"type": "string"}}}}


async def _spool(request: Request, suffix):
    if int(request.headers.get("content-length") or 0) <= 0:
        raise HTTPException(400, "请求体为空，用 --data-binary 上传文件")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        async for chunk in request.stream():
            tmp.write(chunk)
    finally:
        tmp.close()
    return tmp.name


def _targets(cfg, service, services):
    names = as_list(services) or ([service] if service else [])
    if not names:
        raise HTTPException(400, "缺少参数 service（或 services=a,b）")
    for n in names:
        find_service(cfg, n)
    return names


@router.post("/api/unit-coverage", summary="收一份单测覆盖率报告（jacoco.xml）",
             response_model=schemas.UnitCoverageResult, responses=ERR,
             openapi_extra={"requestBody": dict(RAW_BODY, description="mvn verify 产出的 jacoco.xml（jacoco-aggregate 的也行）。curl 用 --data-binary @file")})
async def unit_coverage(request: Request,
                        service: str | None = Query(None, description="服务名"),
                        services: str | None = Query(None, description="一份报告落多个服务，逗号分隔（一个仓库多个服务时用）"),
                        version: str | None = Query(None, description="版本标识，不给则取服务当前 version"),
                        group: str | None = Query(None, description="聚合报告里只取这个模块（<group name=artifactId>）"),
                        cfg: dict = Depends(get_cfg)):
    """构建流水线在 `mvn verify` 之后把聚合报告 POST 过来。hub 解析计数器入库，XML 原文留在
    `data/<svc>/unit/<version>/`；该版本已有 diff 时顺手算出**单测的新增代码覆盖率**。
    同版本重传覆盖。"""
    names = _targets(cfg, service, services)
    path = await _spool(request, ".xml")
    try:
        results = await run_in_threadpool(_store_units, cfg, names, version, path, group)
    finally:
        os.unlink(path)
    if len(names) == 1:
        body = {"ok": True, "service": names[0], **results[0]}
    else:
        body = {"ok": True, "services": names, "results": results}
    code = 200 if all(r.get("ok", True) for r in results) else 400
    return PrettyJSONResponse(body, status_code=code)


def _store_units(cfg, names, version, path, group):
    out = []
    for n in names:
        with LOCK, capture_logs() as lines:
            try:
                r = ops.unit_coverage(cfg, n, version, path, group=group)
                r["service"] = n
                r["log"] = "".join(lines)
            except Exception as exc:          # CovhubError 与解析错误都归 400
                lines.append("[covhub] %s\n" % exc)
                r = {"ok": False, "service": n, "error": str(exc), "log": "".join(lines)}
        out.append(r)
    return out


@router.post("/api/diff", summary="收一份 git diff，用于新增代码覆盖率",
             response_model=schemas.DiffResult, responses=ERR,
             openapi_extra={"requestBody": dict(RAW_BODY, description="`git diff <基线>..<本次>` 的输出。curl 用 --data-binary @file")})
async def push_diff(request: Request,
                    service: str | None = Query(None, description="服务名"),
                    services: str | None = Query(None, description="一份 diff 落多个服务，逗号分隔"),
                    version: str | None = Query(None, description="版本标识，不给则取服务当前 version"),
                    base: str = Query(..., description="基线：上一版的 commit / tag（建议传 rev-parse 后的 SHA）"),
                    head: str | None = Query(None, description="本次的 commit"),
                    cfg: dict = Depends(get_cfg)):
    """推荐的生成命令：
    `git -c core.quotepath=false diff --no-color --no-ext-diff -M --unified=0 --diff-filter=AMR <base>..<head> -- '*.java' '*.kt'`。

    hub 记下每个源码文件的新增行号，之后每次快照都按它算**新增代码的运行时覆盖率**；
    该版本已有的快照与单测报告立刻重算一遍。分母是新增行里 JaCoCo 有探针的行。
    返回体里的 `matchesCurrentVersion` 为 false 说明版本串对不上服务当前 version ——
    运行时快照按 version 找 diff，对不上就永远算不出新增覆盖。"""
    names = _targets(cfg, service, services)
    path = await _spool(request, ".diff")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    finally:
        os.unlink(path)
    results = await run_in_threadpool(_store_diffs, cfg, names, version, base, head, text)
    if len(names) == 1:
        body = {"ok": results[0].get("ok", True), "service": names[0], **results[0]}
        return PrettyJSONResponse(body, status_code=200 if body["ok"] else 400)
    body = {"ok": all(r.get("ok", True) for r in results), "services": names, "results": results}
    return PrettyJSONResponse(body, status_code=200 if body["ok"] else 400)


def _store_diffs(cfg, names, version, base, head, text):
    out = []
    for n in names:
        with LOCK, capture_logs() as lines:
            try:
                r = ops.push_diff(cfg, n, version, base, text, head=head)
                r["service"] = n
                r["log"] = "".join(lines)
            except Exception as exc:
                lines.append("[covhub] %s\n" % exc)
                r = {"ok": False, "service": n, "error": str(exc), "log": "".join(lines)}
        out.append(r)
    return out


@router.post("/api/recompute", summary="按已有 diff 重算某版本的新增代码覆盖", responses=ERR)
def recompute(service: str = Query(..., description="服务名"),
              version: str | None = Query(None, description="版本标识，不给则取服务当前 version"),
              cfg: dict = Depends(get_cfg)):
    find_service(cfg, service)
    with LOCK, capture_logs() as lines:
        try:
            out = ops.recompute_incremental(cfg, service, version)
        except Exception as exc:
            return error(409, str(exc), log="".join(lines))
    return PrettyJSONResponse({"ok": True, "service": service, "log": "".join(lines), **out})
