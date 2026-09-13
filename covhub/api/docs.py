"""在线接口文档：hub 自己托管 Swagger UI（/docs），资源在包里（webui/swagger/），不引 CDN。

页面不要令牌（spec 本身也是公开的），但「Try it out」打带令牌的接口时要填一次 X-Covhub-Token
—— 右上角 Authorize 里填，Swagger UI 会替每个请求带上头。
"""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from .static import NO_CACHE, WEB_DIR

router = APIRouter(include_in_schema=False)

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
    <a href="./">← 回看板</a>
    带令牌的接口先点右侧 <b>Authorize</b> 填 <code>X-Covhub-Token</code>；写接口会真的执行（dump / predeploy 会改数据）。
  </div>
  <div id="swagger-ui"></div>
  <script src="./swagger/swagger-ui-bundle.js"></script>
  <script src="./swagger/swagger-ui-standalone-preset.js"></script>
  <script>
    window.ui = SwaggerUIBundle({
      url: "./api/openapi.json",
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
def docs_page():
    if not (WEB_DIR / "swagger" / "swagger-ui-bundle.js").is_file():
        return HTMLResponse("<p>还没构建前端（cd web && npm run build），Swagger UI 的资源不在包里。"
                            "接口描述仍可从 <a href='./api/openapi.json'>api/openapi.json</a> 取。</p>",
                            status_code=503, headers=NO_CACHE)
    return HTMLResponse(PAGE, headers=NO_CACHE)
