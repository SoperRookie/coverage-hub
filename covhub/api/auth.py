"""令牌门禁。

serve.token（或环境变量 COVHUB_TOKEN）是控制面唯一的门禁：写接口能 --reset
清零计数器，静态目录底下有线上跑的字节码和不可再生的 exec。三种带法：
X-Covhub-Token 头（推荐，不进 access log 和 ps）、?token=（浏览器第一次打开
看板用，hub 种 Cookie 后 302 跳回干净地址）、Cookie（之后的静态请求靠它）。
"""

import secrets
import urllib.parse

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyCookie, APIKeyHeader, APIKeyQuery
from starlette.responses import RedirectResponse

from ..config import token as config_token
from ..runtime import load_runtime

# 浏览器里点开报告时带令牌用的 Cookie。报告页里全是相对链接，不可能每条都
# 挂上 ?token=，所以带对一次就种下它，后续静态请求靠它放行。
TOKEN_COOKIE = "covhub_token"

token_header = APIKeyHeader(
    name="X-Covhub-Token", auto_error=False, scheme_name="tokenHeader",
    description="推荐。令牌走请求头而不是 URL，免得被 access log 和 ps 输出记下来")
token_query = APIKeyQuery(
    name="token", auto_error=False, scheme_name="tokenQuery",
    description="浏览器里访问看板时用；hub 会种一个 Cookie 再跳回不带令牌的地址")
token_cookie = APIKeyCookie(
    name=TOKEN_COOKIE, auto_error=False, scheme_name="tokenCookie",
    description="?token= 带对一次之后由 hub 自动种下，浏览器后续请求靠它")


def token_ok(given, expected):
    """用 compare_digest 而不是 == —— 逐字符短路会泄漏正确的前缀长度。

    比的是 bytes：token 里出现非 ASCII 时 compare_digest 的 str 形式会直接抛错。
    """
    return secrets.compare_digest(str(given or "").encode("utf-8"),
                                  str(expected or "").encode("utf-8"))


def get_cfg(request: Request):
    """每个请求重读配置与服务列表 —— retarget / service update 之后立即生效。"""
    return load_runtime(request.app.state.cfg_path)


def require_token(hdr: str | None = Security(token_header),
                  q: str | None = Security(token_query),
                  ck: str | None = Security(token_cookie),
                  cfg: dict = Depends(get_cfg)):
    expected = config_token(cfg)
    if not expected:
        return
    given = hdr or q or (urllib.parse.unquote(ck) if ck else "")
    if not token_ok(given, expected):
        raise HTTPException(401, "令牌无效或缺失")


def static_gate(request: Request, cfg: dict):
    """静态目录的门禁：放行返回 None，否则返回一个该直接发出去的响应。

    与 /api/* 的差别只有一条：query 里带对了令牌就种 Cookie 并 302 跳回干净地址。
    令牌留在地址栏会被浏览器历史和 Referer 一起带走，所以只让它在这一次请求里出现。
    """
    expected = config_token(cfg)
    if not expected:
        return None
    from_query = request.query_params.get("token", "")
    if from_query and token_ok(from_query, expected):
        query = [(k, v) for k, v in request.query_params.multi_items() if k != "token"]
        target = request.url.path
        if query:
            target += "?" + urllib.parse.urlencode(query)
        resp = RedirectResponse(target, status_code=302)
        resp.set_cookie(TOKEN_COOKIE, urllib.parse.quote(expected), path="/",
                        httponly=True, samesite="strict")
        return resp
    given = (request.headers.get("X-Covhub-Token") or request.cookies.get(TOKEN_COOKIE)
             or from_query)
    if not token_ok(urllib.parse.unquote(given or ""), expected):
        raise HTTPException(401, {
            "error": "令牌无效或缺失",
            "hint": "浏览器：在地址后加 ?token=<serve.token>，之后靠 Cookie 放行；"
                    "命令行：带 X-Covhub-Token 头",
        })
    return None
