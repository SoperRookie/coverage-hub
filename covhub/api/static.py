"""静态文件：dataDir 里的报告，以及（可选的）自托管前端产物。

2.3 起前后端分离部署：前端产物不再随 Python 包分发，默认由 nginx 之类的静态服务器
托管，hub 只剩 API + dataDir。**但自托管的能力保留** —— `serve.webDir` 指向解包后的
产物目录就照旧托管，单机小规模部署不必为了一个看板去装 nginx。

顺序仍是**产物优先、dataDir 其次**：产物是有限集合（index.html、assets/*），被它遮住的
dataDir 路径可枚举；反过来一个叫 assets 的服务会把面板打瘸（ServiceSpec 已把这些名字
列为保留名）。

门禁：面板产物**免令牌** —— 否则配了 token 的首页就是一段 401 JSON，SPA 根本加载不
出来；产物是公开的构建产物，不含秘密。dataDir 照旧拦：底下有线上跑的字节码和不可再
生的 exec。`?token=` 种 Cookie 再 302 的逻辑两边都保留 —— 直接分享出去的报告链接还
靠它（看板自己的登录走 POST /api/login）。

不给未知路径回落到 index.html：push-runtime.sh、Jenkins 库用 curl -sSf 取
/<svc>/versions/<v>/jacoco.xml，404 才是它们要的失败信号。
"""

import html
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from ..config import load_config
from .auth import static_gate
from .responses import error

router = APIRouter(include_in_schema=False)

# 包里自带的后端资源（目前只有 /docs 用的 swagger-ui）。前端产物不在这里 ——
# 它是独立交付物，只有配了 serve.webDir 才由 hub 托管。
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

NO_CACHE = {"Cache-Control": "no-cache"}
IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}

MEDIA = {".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
         ".xml": "application/xml; charset=utf-8", ".csv": "text/csv; charset=utf-8",
         ".json": "application/json; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "application/javascript; charset=utf-8", ".map": "application/json",
         ".gif": "image/gif", ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon",
         ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
         ".webmanifest": "application/manifest+json", ".txt": "text/plain; charset=utf-8",
         ".exec": "application/octet-stream", ".class": "application/java-vm",
         ".diff": "text/plain; charset=utf-8"}


def get_hub_cfg(request: Request):
    # 静态目录只要 dataDir 和令牌，不必查库
    return load_config(request.app.state.cfg_path)


def web_dir(cfg):
    """自托管前端的产物目录；没配就是 None（纯后端形态）。"""
    configured = (cfg.get("serve") or {}).get("webDir")
    return Path(configured) if configured else None


def _grant_if_token(request, cfg):
    """面板产物免令牌，但 ?token= 带对了照样种 Cookie 并 302；带错了就当没带，让 SPA 自己去撞 401。"""
    if "token" not in request.query_params:
        return None
    try:
        granted = static_gate(request, cfg)
    except HTTPException:
        return None
    return granted if isinstance(granted, RedirectResponse) else None


def _inside(root, path):
    """resolve 之后必须仍在 root 里（也挡住符号链接逃逸）；越界返回 None。"""
    target = (root / path).resolve() if path else root
    if target != root and root not in target.parents:
        return None
    return target


@router.get("/{path:path}")
@router.head("/{path:path}")
def static(path: str, request: Request, cfg: dict = Depends(get_hub_cfg)):
    if path.startswith("api/") or path == "api":
        return error(404, "未知接口")

    # 1. 自托管的面板产物（免令牌）。带对 ?token= 仍然种 Cookie 并跳回干净地址，
    #    这样浏览器第一次打开 /?token=xxx 之后，SPA 的 /api/* 请求就有 Cookie 了
    configured = web_dir(cfg)
    web = configured.resolve() if configured is not None and configured.is_dir() else None
    if web is not None:
        target = _inside(web, path)
        if target is not None and target.is_file():
            granted = _grant_if_token(request, cfg)
            if granted is not None:
                return granted
            headers = IMMUTABLE if path.startswith("assets/") else NO_CACHE
            return FileResponse(str(target), media_type=_media_type(target), headers=headers)
        if not path:
            index = web / "index.html"
            if index.is_file():
                granted = _grant_if_token(request, cfg)
                if granted is not None:
                    return granted
                return FileResponse(str(index), media_type=MEDIA[".html"], headers=NO_CACHE)

    # 没配 webDir 时的根路径：给一页说明。返回 401 JSON 或 dataDir 的目录列表都会让人
    # 以为部署坏了 —— 前后端分离下打开 hub 的根本来就不该看到看板。
    # 仍然先认 ?token=：老文档教人用 /?token= 开局，带对了就该拿到 Cookie
    if not path and web is None:
        granted = _grant_if_token(request, cfg)
        if granted is not None:
            return granted
        return HTMLResponse(_hint_page(), headers=NO_CACHE)

    # 2. dataDir（受令牌门禁）
    denied = static_gate(request, cfg)
    if denied is not None:
        return denied
    root = Path(cfg["dataDir"]).resolve()
    target = _inside(root, path)
    if target is None or not target.exists():
        return error(404, "未知路径")

    if target.is_dir():
        # 目录不带斜杠先补上：报告页里全是相对链接，路径少一层斜杠全都会指错
        if path and not request.url.path.endswith("/"):
            return RedirectResponse(request.url.path + "/"
                                    + ("?" + request.url.query if request.url.query else ""),
                                    status_code=301)
        index = target / "index.html"
        if index.is_file():
            return FileResponse(str(index), media_type=MEDIA[".html"], headers=NO_CACHE)
        return HTMLResponse(_listing(target, request.url.path), headers=NO_CACHE)
    # current/ 每次采集都会被重写，别让浏览器按启发式缓存拿到几分钟前的报告
    headers = NO_CACHE if "/current/" in ("/" + path) or path.startswith("current/") else None
    return FileResponse(str(target), media_type=_media_type(target), headers=headers)


HINT_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>covhub</title><style>
body { font: 14px -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
       margin: 60px auto; max-width: 640px; color: #2f333a; line-height: 1.8; padding: 0 20px; }
code { background: #f4f5f7; padding: 1px 5px; border-radius: 3px; }
a { color: #2a78d6; }
</style></head><body>
<h1>covhub</h1>
<p>这里是 hub 的<strong>控制 API 与报告目录</strong>，看板是独立部署的前端。</p>
<ul>
  <li>接口文档：<a href="/docs">/docs</a></li>
  <li>探活：<a href="/api/health">/api/health</a></li>
  <li>报告与 <code>jacoco.xml</code>：<code>/&lt;服务&gt;/current/</code>、<code>/&lt;服务&gt;/versions/&lt;版本&gt;/</code>（要令牌）</li>
</ul>
<p>看板应该指向你自己部署的前端站点（<code>integration/nginx/covhub.conf</code> 是模板）。
想让 hub 自己把看板一起托管，就把 <code>serve.webDir</code> 指向前端产物目录。</p>
</body></html>
"""


def _hint_page():
    return HINT_PAGE


def _media_type(target):
    return MEDIA.get(target.suffix.lower())


def _listing(target, url_path):
    """最简目录列表：versions/ 这类目录没有 index.html，能点进去翻比什么都没有强。"""
    names = sorted(os.listdir(target))
    rows = []
    if url_path.strip("/"):
        rows.append('<li><a href="../">../</a></li>')
    for name in names:
        full = target / name
        shown = name + ("/" if full.is_dir() else "")
        rows.append('<li><a href="%s">%s</a></li>' % (html.escape(shown, quote=True), html.escape(shown)))
    return ("<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>%s</title></head>"
            "<body><h1>%s</h1><ul>%s</ul></body></html>"
            % (html.escape(url_path), html.escape(url_path), "".join(rows)))
