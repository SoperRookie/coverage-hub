"""响应格式与异常映射。

JSON 一律 indent=2 —— covhub-client.sh 的 wait-online 和 Jenkins 库的 online()
都是 grep '"online": true'（冒号后带空格）来判断的，换成紧凑格式会把它们
静默打断。所有异常处理器也走同一个响应类，形态统一为 {ok: false, error}。
"""

import json

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..errors import ConfigError, CovhubError, ServiceNotFound


class PrettyJSONResponse(JSONResponse):
    def render(self, content):
        return json.dumps(content, ensure_ascii=False, indent=2).encode("utf-8")


def error(status, message, **extra):
    body = {"ok": False, "error": message}
    body.update(extra)
    return PrettyJSONResponse(body, status_code=status)


def install_handlers(app: FastAPI):
    @app.exception_handler(ServiceNotFound)
    async def _not_found(_request, exc):
        return error(404, str(exc))

    @app.exception_handler(ConfigError)
    async def _config(_request, exc):
        return error(500, "配置文件读取失败：%s" % exc)

    @app.exception_handler(CovhubError)
    async def _business(_request, exc):
        # 业务上失败了（目标不可达、没有数据……）：409，让流水线非零退出
        return error(409, str(exc))

    @app.exception_handler(RequestValidationError)
    async def _validation(_request, exc):
        # 缺参数 / 类型不对是 400，不是 FastAPI 默认的 422 —— 客户端只认「非 2xx 即失败」，
        # 但文档里 400 一直是「缺少必需参数」的语义
        parts = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err.get("loc", ()) if x not in ("query", "body"))
            msg = err.get("msg", "")
            if msg.startswith("Value error, "):
                msg = msg[len("Value error, "):]
            parts.append("%s：%s" % (loc or "参数", msg))
        return error(400, "；".join(parts) or "参数不合法")

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request, exc: StarletteHTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            body = {"ok": False}
            body.update(detail)
            return PrettyJSONResponse(body, status_code=exc.status_code, headers=exc.headers)
        if exc.status_code == 404 and detail == "Not Found":
            detail = "未知接口"
        if exc.status_code == 405 and detail == "Method Not Allowed":
            detail = "该接口不支持这个方法"
        return PrettyJSONResponse({"ok": False, "error": str(detail)},
                                  status_code=exc.status_code, headers=exc.headers)

    # FastAPI 自己的 HTTPException 是 Starlette 的子类，上面那条已经覆盖；
    # 显式注册一次免得以后有人给它单独加处理器时顺序出问题
    app.add_exception_handler(HTTPException, _http)
