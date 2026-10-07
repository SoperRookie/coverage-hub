"""构建期送进来的东西：单测 jacoco.xml、git diff、源码。

几个接口的正文都是原始文件，照 upload-classes 的方式流式落盘：不挂 merged_params、
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


@router.post("/api/diff", summary="该版本的 diff：由 hub 比对两版源码生成（from=sources），或收流水线上传的 git diff",
             response_model=schemas.DiffResult, responses=ERR,
             openapi_extra={"requestBody": dict(RAW_BODY, required=False,
                                                description="`git diff <基线>..<本次>` 的输出。curl 用 --data-binary @file。"
                                                            "from=sources 时不读正文")})
async def push_diff(request: Request,
                    service: str | None = Query(None, description="服务名"),
                    services: str | None = Query(None, description="一份 diff 落多个服务，逗号分隔"),
                    version: str | None = Query(None, description="版本标识，不给则取服务当前 version"),
                    base: str | None = Query(None, description="基线。上传 git diff 时必填，是上一版的 commit / tag"
                                                               "（建议传 rev-parse 后的 SHA）；from=sources 时是基线的"
                                                               "**版本标识**，不给则 hub 自动定：服务当前 version → "
                                                               "最近结算的版本 → 最近上传过源码的版本"),
                    head: str | None = Query(None, description="本次的 commit（上传 git diff 时）"),
                    from_: str | None = Query(None, alias="from",
                                              description="填 sources：不读正文，由 hub 比对该版本与基线版本经 upload-sources "
                                                          "传上来的源码生成 diff（git diff --no-index -M，识别重命名）"),
                    cfg: dict = Depends(get_cfg)):
    """两种来源，效果一样：hub 记下每个源码文件的新增行号，之后每次快照都按它算**新增代码的
    运行时覆盖率**，该版本已有的快照与单测报告立刻重算一遍。分母是新增行里 JaCoCo 有探针的行。

    **from=sources（推荐）**：两版源码都经 upload-sources 传上来后由 hub 比对，构建节点不需要
    基线 commit 的历史（浅克隆、清过的工作区都无所谓）。upload-sources 默认就会做这一步，这个
    接口用于显式指定基线或重做。

    **上传 git diff**：推荐的生成命令
    `git -c core.quotepath=false diff --no-color --no-ext-diff -M --unified=0 --diff-filter=AMR <base>..<head> -- '*.java' '*.kt'`。
    流水线上传的 diff 优先：之后 upload-sources 不会用自动生成的覆盖它。

    返回体里的 `matchesCurrentVersion` 为 false 说明版本串对不上服务当前 version ——
    运行时快照按 version 找 diff，对不上就永远算不出新增覆盖。"""
    names = _targets(cfg, service, services)
    if from_ == "sources":
        results = await run_in_threadpool(_diffs_from_sources, cfg, names, version, base)
        return _multi(names, results)
    if from_:
        return error(400, "from 只能是 sources")
    if not base:
        return error(400, "缺少 base（上传 git diff 时是基线的 commit / tag；想让 hub 比对源码生成就带 from=sources）")
    path = await _spool(request, ".diff")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    finally:
        os.unlink(path)
    results = await run_in_threadpool(_store_diffs, cfg, names, version, base, head, text)
    return _multi(names, results)


def _multi(names, results):
    """一个服务时平铺返回，多个服务时按服务列出，任一失败整体 400。"""
    if len(names) == 1:
        body = {"ok": results[0].get("ok", True), "service": names[0], **results[0]}
        return PrettyJSONResponse(body, status_code=200 if body["ok"] else 400)
    body = {"ok": all(r.get("ok", True) for r in results), "services": names, "results": results}
    return PrettyJSONResponse(body, status_code=200 if body["ok"] else 400)


def _each(cfg, names, fn):
    """逐服务在锁内执行 fn(name)，日志与异常都收进该服务自己的返回体。"""
    out = []
    for n in names:
        with LOCK, capture_logs() as lines:
            try:
                r = fn(n)
                r["service"] = n
                r["log"] = "".join(lines)
            except Exception as exc:          # 版本串、压缩包格式、不安全路径、没基线都归 400
                lines.append("[covhub] %s\n" % exc)
                r = {"ok": False, "service": n, "error": str(exc), "log": "".join(lines)}
        out.append(r)
    return out


def _store_diffs(cfg, names, version, base, head, text):
    return _each(cfg, names, lambda n: ops.push_diff(cfg, n, version, base, text, head=head))


def _diffs_from_sources(cfg, names, version, base):
    return _each(cfg, names, lambda n: ops.diff_from_sources(cfg, n, version, base))


@router.post("/api/upload-sources", summary="上传该版本的源码（报告下钻到行、新增代码看源码）",
             response_model=schemas.SourcesUploadResult, responses=ERR,
             openapi_extra={"requestBody": {
                 "required": True,
                 "description": "源码压缩包，tar.gz 或 zip，从仓库根打包（covhub-client.sh upload-sources "
                                "会替你打）。curl 用 --data-binary @file",
                 "content": {
                     "application/gzip": {"schema": {"type": "string", "format": "binary"}},
                     "application/zip": {"schema": {"type": "string", "format": "binary"}},
                     "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
                 }}})
async def upload_sources(request: Request,
                         service: str | None = Query(None, description="服务名"),
                         services: str | None = Query(None, description="一份源码落多个服务，逗号分隔（一个仓库多个服务时用）"),
                         version: str | None = Query(None, description="版本标识，不给则取服务当前 version"),
                         diff: str = Query("auto", description="auto：存好源码后由 hub 比对基线版本的源码生成这一版的 diff"
                                                               "（已有流水线上传的 diff 时不覆盖）；skip：不生成"),
                         base: str | None = Query(None, description="生成 diff 时的基线版本标识，不给则 hub 自动定："
                                                                    "服务当前 version → 最近结算的版本 → 最近上传过源码的版本"),
                         cfg: dict = Depends(get_cfg)):
    """hub 独立部署时本机没有源码：流水线按版本把源码传上来，存在 `data/<svc>/sources/<版本>/`。
    出报告按版本取（JaCoCo HTML 生成时把源码内嵌进类页面，归档后不再依赖它），新增代码视图
    能看整个文件，历史版本也对得上。

    **顺带生成这一版的 diff**（新增代码覆盖率的依据）：hub 拿基线版本的源码和这一份比对，构建
    节点不需要有基线 commit 的历史。生成不了（第一次接入没有基线、hub 没装 git）不算失败，
    返回体 `diff` 为 null、`diffReason` 说明原因。

    只收 `.java/.kt/.groovy/.scala` 且丢掉 `src/test/`，其余文件（配置、密钥）一律不落盘；
    源码根按每个文件的 `package` 声明识别，与目录布局无关。同版本重传整份替换。
    这个目录**不经静态路径外发**，只通过报告与源码视图接口出去。"""
    names = _targets(cfg, service, services)
    if diff not in ("auto", "skip"):
        return error(400, "diff 只能是 auto 或 skip")
    path = await _spool(request, ".upload")
    try:
        results = await run_in_threadpool(
            _each, cfg, names, lambda n: ops.upload_sources(cfg, n, version, path, diff=diff, base=base))
    finally:
        os.unlink(path)
    return _multi(names, results)


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
