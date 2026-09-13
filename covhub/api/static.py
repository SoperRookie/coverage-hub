"""dataDir 的静态服务 —— 看板首页、JaCoCo 原生报告、jacoco.xml 都从这里出。

配了令牌就必须和 /api/ 一起拦：这底下不只有报告，artifacts/ 是线上跑的那份
字节码（反编译即源码），exec/ 是不可再生的执行轨迹。只护住 /api/ 而把整棵树
敞开，等于那道门白装。

不用 StaticFiles 挂载：它跑在依赖注入之外，拿不到「?token= 种 Cookie 再 302」
那套逻辑。
"""

import html
import os
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from ..config import load_config
from .auth import static_gate
from .responses import error

router = APIRouter(include_in_schema=False)


def get_hub_cfg(request: Request):
    # 静态目录只要 dataDir 和令牌，不必查库
    return load_config(request.app.state.cfg_path)


@router.get("/{path:path}")
@router.head("/{path:path}")
def static(path: str, request: Request, cfg: dict = Depends(get_hub_cfg)):
    if path.startswith("api/") or path == "api":
        return error(404, "未知接口")
    denied = static_gate(request, cfg)
    if denied is not None:
        return denied

    root = Path(cfg["dataDir"]).resolve()
    target = (root / path).resolve() if path else root
    # 防穿越：resolve 之后必须仍在 dataDir 里（也挡住符号链接逃逸）。拒绝统一 404，
    # 别用 403 泄露目录结构
    if target != root and root not in target.parents:
        return error(404, "未知路径")
    if not target.exists():
        return error(404, "未知路径")

    if target.is_dir():
        # 目录不带斜杠先补上：报告页里全是相对链接，路径少一层斜杠全都会指错
        if path and not request.url.path.endswith("/"):
            return RedirectResponse(request.url.path + "/"
                                    + ("?" + request.url.query if request.url.query else ""),
                                    status_code=301)
        index = target / "index.html"
        if index.is_file():
            return FileResponse(str(index), media_type="text/html; charset=utf-8")
        return HTMLResponse(_listing(target, request.url.path))
    return FileResponse(str(target), media_type=_media_type(target))


def _media_type(target):
    ext = target.suffix.lower()
    return {".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
            ".xml": "application/xml; charset=utf-8", ".csv": "text/csv; charset=utf-8",
            ".json": "application/json; charset=utf-8", ".css": "text/css",
            ".js": "application/javascript", ".gif": "image/gif", ".png": "image/png",
            ".exec": "application/octet-stream", ".class": "application/java-vm",
            ".txt": "text/plain; charset=utf-8"}.get(ext)


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
