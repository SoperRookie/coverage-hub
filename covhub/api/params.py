"""POST 接口的参数：query string、JSON body、form body 三种写法都认。

1.x 就是这么宽松的（covhub-client.sh 用 query，Jenkins 库也用 query，手工 curl
常用 -d）。这里把三处合并成一个 dict，query 优先。上传接口的正文是二进制压缩包，
**绝不能**挂这个依赖。
"""

import json
import urllib.parse

from fastapi import Request


async def merged_params(request: Request) -> dict:
    params = {k: v for k, v in request.query_params.multi_items()}   # 重复键取最后一个
    ctype = (request.headers.get("content-type") or "").lower()
    if request.method == "POST" and (request.headers.get("content-length") or "0") != "0":
        raw = (await request.body()).decode("utf-8", "replace").strip()
        if raw.startswith("{"):
            try:
                body = json.loads(raw)
            except ValueError:
                body = {}
            if isinstance(body, dict):
                for k, v in body.items():
                    params.setdefault(k, v)
        elif raw and ("form-urlencoded" in ctype or "=" in raw):
            for k, v in urllib.parse.parse_qs(raw).items():
                params.setdefault(k, v[-1])
    return params


def truthy(value):
    return str(value if value is not None else "").lower() in ("1", "true", "yes")


def as_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return [str(p) for p in value]
    return [p.strip() for p in str(value).split(",") if p.strip()]
