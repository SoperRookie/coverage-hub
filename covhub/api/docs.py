"""在线接口文档：hub 自己托管 Swagger UI（/docs），资源在包里（covhub/static/swagger/），不引 CDN。

资源由本模块自己的 /swagger/<文件> 路由发出，**不经过兜底静态路由** —— 2.3 起前端产物
是独立交付物，前后端分离部署时 hub 根本没有 webDir，接口文档不能跟着一起失效。

OpenAPI 描述直接内嵌在页面里，不再单独开 /api/openapi.json —— 只有这一个入口看文档。
页面不要令牌，但「Try it out」打带令牌的接口时要填一次 X-Covhub-Token（右上角 Authorize），
Swagger UI 会替每个请求带上头。
"""
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse

from .static import MEDIA, NO_CACHE, STATIC_DIR, get_hub_cfg, web_dir
from .responses import error

router = APIRouter(include_in_schema=False)

SWAGGER_DIR = STATIC_DIR / "swagger"
# 白名单：这个目录只该发这四个文件，不做通配（它和 dataDir 不一样，没有令牌门禁）
SWAGGER_FILES = ("swagger-ui.css", "swagger-ui-bundle.js",
                 "swagger-ui-standalone-preset.js", "favicon-32x32.png")

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>covhub 接口文档</title>
  <link rel="icon" href="./swagger/favicon-32x32.png">
  <link rel="stylesheet" href="./swagger/swagger-ui.css">
  <style>
    body { margin: 0; }
    .swagger-ui .topbar { display: none; }
    .covhub-bar { font: 13px -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
                  padding: 10px 20px; background: #f4f5f7; border-bottom: 1px solid #e4e6ea; color: #4b5059; }
    .covhub-bar a { color: #2a78d6; text-decoration: none; margin-right: 14px; }
  </style>
</head>
<body>
  <div class="covhub-bar">
    __HOME__带令牌的接口先点右侧 <b>Authorize</b> 填 <code>X-Covhub-Token</code>；写接口会真的执行（dump / predeploy 会改数据）。
  </div>
  <div id="swagger-ui"></div>
  <script src="./swagger/swagger-ui-bundle.js"></script>
  <script src="./swagger/swagger-ui-standalone-preset.js"></script>
  <script>
    window.ui = SwaggerUIBundle({
      spec: __SPEC__,
      dom_id: "#swagger-ui",
      presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
      layout: "BaseLayout",
      docExpansion: "list",
      defaultModelsExpandDepth: 0,
      displayRequestDuration: true,
      persistAuthorization: true,
      tryItOutEnabled: false,
    });
  </script>
</body>
</html>
"""


@router.get("/docs")
def docs_page(request: Request, cfg: dict = Depends(get_hub_cfg)):
    if not (SWAGGER_DIR / "swagger-ui-bundle.js").is_file():
        return HTMLResponse("<p>Swagger UI 的资源不在包里（covhub/static/swagger/）。"
                            "接口描述可用 <code>covhub openapi</code> 导出成文件。</p>",
                            status_code=503, headers=NO_CACHE)
    # 内嵌进 <script>：把 </ 断开，免得 spec 里的字符串提前关掉标签
    spec = json.dumps(request.app.openapi(), ensure_ascii=False).replace("</", "<\\/")
    # 分离部署时 hub 的根不是看板，这个链接只在自托管（配了 webDir）时才给
    home = '<a href="/">← 回看板</a> ' if web_dir(cfg) is not None else ""
    return HTMLResponse(PAGE.replace("__SPEC__", spec).replace("__HOME__", home), headers=NO_CACHE)


@router.get("/swagger/{name}")
def swagger_asset(name: str):
    """Swagger UI 的静态资源。和 /docs 一样不要令牌 —— 它们是公开的第三方构建产物。"""
    if name not in SWAGGER_FILES:
        return error(404, "未知路径")
    target = SWAGGER_DIR / name
    if not target.is_file():
        return error(404, "未知路径")
    return FileResponse(str(target), media_type=MEDIA.get(target.suffix.lower()), headers=NO_CACHE)
