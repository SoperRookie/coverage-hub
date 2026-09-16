"""远程控制 API（1.x 的 11 条接口，路径与返回体保持逐字段兼容）。

写接口在 hub 内部串行执行（LOCK），返回体里带着这次执行的日志；非 2xx 一律
表示失败，调用方应据此让部署流程停下来。
"""

import os
import tempfile
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, PlainTextResponse
from starlette.background import BackgroundTask

from .. import __version__, ops
from ..artifacts import pack_classes, store_classes
from ..config import find_service, load_config
from ..config import token as config_token
from ..db import repo
from ..errors import CovhubError
from ..locks import LOCK
from ..logbuf import capture_logs, log
from . import schemas
from .auth import TOKEN_COOKIE, get_cfg, require_token, token_header, token_ok
from .params import as_list, merged_params, truthy
from .responses import PrettyJSONResponse, error

open_router = APIRouter(tags=["探活"])
# 登录也不要令牌（它就是来验令牌的），但和探活不是一类，单独挂一个
session_router = APIRouter(tags=["会话"])
router = APIRouter(dependencies=[Depends(require_token)])

ERR = {401: {"model": schemas.Error, "description": "令牌无效或缺失"},
       404: {"model": schemas.Error, "description": "没有这个服务"},
       500: {"model": schemas.Error, "description": "hub 内部错误"}}
WRITE = {**ERR, 409: {"model": schemas.CommandResult,
                      "description": "业务上失败了（目标不可达、没有数据……），log 里有原因"}}


def run_command(fn, cfg, name, **kw):
    """在锁内执行操作，把它打印的日志一起回给调用方。

    业务失败（CovhubError）翻译成 409 —— 让流水线那边非零退出，而不是拿到
    一个"成功"的空响应继续往下走。
    """
    with LOCK, capture_logs() as lines:
        try:
            fn(cfg, name, **kw)
            code = 200
        except CovhubError as exc:
            lines.append("[covhub] %s\n" % exc)
            code = 409
        except SystemExit as exc:            # 库函数不该抛它，兜底
            code = 409 if exc.code else 200
        except Exception as exc:
            lines.append("[covhub] 错误：%s\n" % exc)
            code = 500
    body = {"ok": code == 200, "service": name, "log": "".join(lines)}
    if code == 200:
        body["latest"] = repo.latest(name)
    return code, body


def command_response(fn, cfg, name, **kw):
    code, body = run_command(fn, cfg, name, **kw)
    return PrettyJSONResponse(body, status_code=code)


# ---- 不要令牌的一条 ----

@open_router.get("/api/health", summary="存活探测", response_model=schemas.Health)
async def health(request: Request):
    """唯一不需要令牌的接口。返回 hub 版本和已登记的服务名列表。

    故意不进线程池：写接口把线程池占满时它仍然要能答话。"""
    try:
        names = repo.list_service_names()
    except Exception:                        # 库连不上也得报活着，服务名给空
        names = []
    return PrettyJSONResponse({"ok": True, "version": __version__, "services": names})


@session_router.post("/api/login", summary="用令牌换一个 Cookie",
                  response_model=schemas.LoginResult,
                  responses={401: {"model": schemas.Error, "description": "令牌无效或缺失"}})
async def login(request: Request, hdr: str | None = Security(token_header)):
    """看板的登录入口：令牌走 X-Covhub-Token 头，对了就种下 Cookie。

    前后端分离部署时看板的首页由 nginx 发，hub 收不到它 —— 靠静态目录 `?token=` 种
    Cookie 的老路走不通了。令牌只出现在这一个请求的头里，不进地址栏、浏览器历史和
    Referer，比原先跳 /?token= 更稳妥。

    报告链接直接分享出去的场景仍然走 `?token=`（见 api/static.py）。
    """
    expected = config_token(load_config(request.app.state.cfg_path))
    if not expected:
        # 没配令牌的 hub 本来就人人可用，种 Cookie 没有意义
        return PrettyJSONResponse({"ok": True, "tokenRequired": False})
    if not token_ok(hdr, expected):
        raise HTTPException(401, "令牌无效或缺失")
    resp = PrettyJSONResponse({"ok": True, "tokenRequired": True})
    resp.set_cookie(TOKEN_COOKIE, urllib.parse.quote(expected), path="/",
                    httponly=True, samesite="strict")
    return resp


# ---- 查询 ----

@router.get("/api/status", tags=["查询"], summary="连通性与最新覆盖率",
            response_model=schemas.StatusResponse, responses=ERR)
def status(service: str | None = Query(None, description="只看某一个服务；不给则全部"),
           cfg: dict = Depends(get_cfg)):
    """看板上那些数字的 JSON 版。push 服务的 unknown=true 表示当前进程没有收集端，
    说不出在线与否 —— 那不等于离线。"""
    return PrettyJSONResponse({"ok": True, "services": ops.status(cfg, service)})


@router.get("/api/agent-opts", tags=["查询"], summary="取该服务应注入的 -javaagent 参数串",
            response_model=schemas.AgentOpts,
            responses={**ERR, 200: {"model": schemas.AgentOpts,
                                         "content": {"text/plain": {"schema": {"type": "string"}}}}})
def agent_opts(service: str = Query(..., description="服务名，须与 hub 配置一致"),
               fmt: str | None = Query(None, alias="format",
                                       description="填 text 则返回纯文本而不是 JSON"),
               cfg: dict = Depends(get_cfg)):
    """被测服务零侵入接入的入口：把返回的参数串塞进 JAVA_TOOL_OPTIONS 即可，
    不改代码不改 pom。加 format=text 直接出纯文本，方便 shell 里 $(curl ...)。"""
    opts = ops.agent_opts(cfg, service)
    if fmt == "text":
        return PlainTextResponse(opts + "\n")
    return PrettyJSONResponse({"ok": True, "service": service, "agentOpts": opts})


@router.get("/api/agent.jar", tags=["查询"], summary="下载 jacocoagent.jar",
            responses={**ERR, 200: {"description": "jar 文件",
                                         "content": {"application/java-archive":
                                                     {"schema": {"type": "string", "format": "binary"}}}},
                                   404: {"model": schemas.Error,
                                         "description": "配置里 jacocoAgent 指向的文件在 hub 上不存在"}})
@router.get("/agent.jar", include_in_schema=False)
def agent_jar(cfg: dict = Depends(get_cfg)):
    """被测机器不必预先铺一份 agent，容器的 initContainer 一条 curl 就能拿到。
    读的是配置里 jacocoAgent 指向的文件 —— 那一项填的是**被测端**路径，
    两者不一致时这个接口会 404，但 agent-opts 照样输出正确的参数串。"""
    jar = cfg["jacocoAgent"]
    if not os.path.isfile(jar):
        return error(404, "找不到 " + jar)
    return FileResponse(jar, media_type="application/java-archive", filename="jacocoagent.jar")


@router.get("/api/diagnose", tags=["查询"], summary="诊断 exec 与 class 产物是否对得上",
            response_model=schemas.DiagnoseResponse, responses=ERR)
def diagnose(service: str = Query(..., description="服务名"),
             version: str | None = Query(None, description="诊断某个已归档版本，不给则诊断当前周期"),
             cfg: dict = Depends(get_cfg)):
    """**任何覆盖率数字不对劲，先跑这个。** 它把 exec 里记录的 class id 和 classfiles
    的 class id 求交集 —— 匹配率低就是 class 产物对不上，这是接入时最贵、最难查、
    而且**不会报错**的一个坑（报告只会显示全部未覆盖）。"""
    with LOCK:
        return PrettyJSONResponse({"ok": True, "diagnose": ops.diagnose(cfg, service, version)})


# ---- 采集 / 发版 ----

def _pick(params, query_value, key):
    return query_value if query_value is not None else params.get(key)


@router.post("/api/dump", tags=["采集"], summary="拉一次快照并出报告",
             response_model=schemas.CommandResult, responses=WRITE)
def dump(service: str | None = Query(None, description="服务名"),
         params: dict = Depends(merged_params), cfg: dict = Depends(get_cfg)):
    """累加，不清零。采完会重新渲染看板。"""
    name = _need_service(cfg, _pick(params, service, "service"))
    return command_response(ops.dump, cfg, name)


@router.post("/api/report", tags=["采集"], summary="用已有 exec 重出报告",
             response_model=schemas.CommandResult, responses=WRITE)
def report(service: str | None = Query(None, description="服务名"),
           params: dict = Depends(merged_params), cfg: dict = Depends(get_cfg)):
    """不碰被测服务，只是拿现有数据重新生成一次报告 —— 改了 reportExcludes 之后用。"""
    name = _need_service(cfg, _pick(params, service, "service"))
    return command_response(ops.report, cfg, name)


@router.post("/api/predeploy", tags=["发版"], summary="结算并归档（停服前必须调用）",
             response_model=schemas.CommandResult, responses=WRITE)
def predeploy(service: str | None = Query(None, description="服务名"),
              version: str | None = Query(None, description="版本标识，不给则取配置里的 version"),
              allowMissing: str | None = Query(None, description="填 1 则目标已离线时不报错"),
              params: dict = Depends(merged_params), cfg: dict = Depends(get_cfg)):
    """dump --reset + 归档 + 出终版报告。

    **必须在停服之前执行。** 服务一停 agent 随进程消失，那段覆盖率没有任何补救手段
    —— 所以目标不可达时它**故意报 409**，好让部署流程停下来。确实要跳过就带
    allowMissing=1，但那意味着这段数据已经丢了。"""
    name = _need_service(cfg, _pick(params, service, "service"))
    return command_response(ops.predeploy, cfg, name,
                       version=_pick(params, version, "version"),
                       allow_missing=truthy(_pick(params, allowMissing, "allowMissing")))


@router.post("/api/retarget", tags=["发版"], summary="把配置指向新版本的 class 产物",
             response_model=schemas.CommandResult, responses=WRITE)
def retarget(service: str | None = Query(None, description="服务名"),
             version: str | None = Query(None, description="新版本标识"),
             classfiles: str | None = Query(
                 None, description="**hub 上**的 class 产物路径，逗号分隔",
                 examples=["./data/order-service/artifacts/1.4.3"]),
             sourcefiles: str | None = Query(None, description="hub 上的源码路径，逗号分隔"),
             params: dict = Depends(merged_params), cfg: dict = Depends(get_cfg)):
    """**发版流水线里最容易漏的一步。** JaCoCo 按 CRC64 class id 匹配，class 产物不跟着
    版本换，新周期采到的 exec 就和旧 class 对不上，报告全是未覆盖。用 upload-classes
    带 retarget=1 可以省掉这一次调用。改的是数据库里的服务配置，立即生效。"""
    name = _need_service(cfg, _pick(params, service, "service"))
    return command_response(ops.retarget, cfg, name,
                       version=_pick(params, version, "version"),
                       classfiles=as_list(_pick(params, classfiles, "classfiles")),
                       sourcefiles=as_list(_pick(params, sourcefiles, "sourcefiles")))


def _need_service(cfg, name):
    if not name:
        raise HTTPException(400, "缺少参数 service")
    find_service(cfg, name)          # 不存在 → ServiceNotFound → 404
    return name


# ---- 产物 ----

@router.post("/api/upload-classes", tags=["产物"], summary="上传该版本的 class 产物压缩包",
             response_model=schemas.UploadResult,
             responses={**ERR, 400: {"model": schemas.Error,
                                     "description": "缺参数、请求体为空、或压缩包里有不安全的路径"}},
             openapi_extra={"requestBody": {
                 "required": True,
                 "description": "压缩包本体，tar.gz 或 zip。curl 用 --data-binary @file",
                 "content": {
                     "application/gzip": {"schema": {"type": "string", "format": "binary"}},
                     "application/zip": {"schema": {"type": "string", "format": "binary"}},
                     "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
                 }}})
async def upload_classes(request: Request,
                         service: str = Query(..., description="服务名"),
                         version: str = Query(..., description="版本标识"),
                         retarget: str | None = Query(None, description="填 1 则上传后把配置指向这份产物"),
                         cfg: dict = Depends(get_cfg)):
    """报告是 hub 出的，所以 class 必须在 hub 上，且必须是**线上跑的那一份**。
    有了这个接口，被测机器和发版节点都不必和 hub 共享文件系统。

    产物优先用 agent 的 classdumpdir 落盘那份 —— 它和 exec 的指纹定义上必然匹配，
    还包含 Spring AOP、MyBatis 代理这类构建产物里根本没有的动态类。

    包里习惯带的一层顶层目录会自动剥掉；绝对路径和跳出目录的成员一律拒绝。
    带 retarget=1 则上传完顺手把配置的 classfiles 指向这份产物 —— **发版时推荐这么用**。"""
    svc = find_service(cfg, service)
    if int(request.headers.get("content-length") or 0) <= 0:
        return error(400, "请求体为空，用 --data-binary 上传压缩包")

    # 正文是二进制，大包直接落盘，不整个读进内存。锁要在线程池里拿 ——
    # 在事件循环里等一把线程锁会把整个服务卡住
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".upload")
    try:
        async for chunk in request.stream():
            tmp.write(chunk)
        tmp.close()
        try:
            dest, count = await run_in_threadpool(_store_locked, cfg, svc, version, tmp.name)
        except Exception as exc:
            return error(400, str(exc))
    finally:
        tmp.close()
        os.unlink(tmp.name)

    body = {"ok": True, "service": service, "version": version, "path": dest, "classes": count}
    if truthy(retarget):
        # retarget=1：上传完直接把配置指向这份产物，省一次调用
        code, out = await run_in_threadpool(run_command, ops.retarget, cfg, service,
                                            version=version, classfiles=[dest])
        body["retarget"] = out["log"]
        if code != 200:
            body["ok"] = False
            return PrettyJSONResponse(body, status_code=code)
    return PrettyJSONResponse(body)


def _store_locked(cfg, svc, version, path):
    with LOCK:
        return store_classes(cfg, svc, version, path)


@router.get("/api/classes", tags=["产物"], summary="取回某个版本的 class 产物",
            responses={**ERR, 
                200: {"description": "tar.gz 包。响应头 X-Covhub-Classes 是 .class 数量",
                      "headers": {"X-Covhub-Classes": {"description": "包里的 .class 数量",
                                                       "schema": {"type": "integer"}},
                                  "Content-Disposition": {"schema": {"type": "string"}}},
                      "content": {"application/gzip": {"schema": {"type": "string", "format": "binary"}}}},
                404: {"model": schemas.Error,
                      "description": "hub 上没有该版本的产物 —— 发版时没跑过 upload-classes，"
                                     "或结算时用的 classfiles 已经不在了"}})
def classes(service: str = Query(..., description="服务名"),
            version: str = Query(..., description="版本标识"),
            cfg: dict = Depends(get_cfg)):
    """打成 tar.gz 回传。在别处重出报告时要的就是**采集时运行的那份 class**
    —— 有了它，发版节点不必自己囤历史产物。

    两个来源按可信度排序：先找 upload-classes 传上来的 artifacts/<版本>/，
    再找结算时 manifest 里记的 classfiles。都没有就 404。"""
    svc = find_service(cfg, service)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz")
    tmp.close()
    try:
        with LOCK:
            count, size = pack_classes(cfg, svc, version, tmp.name)
    except RuntimeError as exc:
        os.unlink(tmp.name)
        return error(404, str(exc))
    except Exception as exc:
        os.unlink(tmp.name)
        return error(500, str(exc))
    log("%s：回传 %s 的 class 产物 %d 个（%.1f MB）" % (service, version, count, size / 1048576.0))
    return FileResponse(
        tmp.name, media_type="application/gzip",
        filename="classes-%s-%s.tar.gz" % (service, version),
        headers={"X-Covhub-Classes": str(count)},
        background=BackgroundTask(os.unlink, tmp.name))
