"""app 工厂与生命周期。

lifespan 里做的事和 1.x 的 cmd_serve 一样：建库、渲染看板、起 push 收集端、
起采集线程；退出时把它们收掉。**必须 uvicorn 单 worker** —— 收集端握着长连接、
采集线程和 API 共用一把进程锁，多 worker 就是多份收集端抢端口、多份采集重复取数。
"""

import contextlib
import threading

from fastapi import FastAPI

from .. import __version__, config
from ..collector import PushCollector, get_collector, set_collector
from ..dashboard import render_dashboard
from ..db import engine
from ..logbuf import log
from ..runtime import load_runtime, prepare_database
from ..watch import watch_loop
from . import routes_control, routes_services, static
from .responses import PrettyJSONResponse, install_handlers

OPEN_ROUTES = ("/api/health", "/api/openapi.json")

DESCRIPTION = (
    "JaCoCo 运行期覆盖率的采集与看板。整套方案只有 hub 这一个服务端，"
    "被测机器和发版节点不装 Python、不装 java、不放配置文件，"
    "全部通过这些接口驱动 hub 干活 —— 它们只需要 curl。\n\n"
    "**写接口在 hub 内部串行执行**，返回体里带着这次执行的日志。"
    "**非 2xx 一律表示失败**，调用方应据此让部署流程停下来。\n\n"
    "发版节点上更省事的做法是用 integration/covhub-client.sh 包一层。"
)

TAGS = [
    {"name": "探活", "description": "不需要令牌"},
    {"name": "查询", "description": "只读，不改任何状态"},
    {"name": "采集", "description": "拉数据、出报告，会写 data/"},
    {"name": "发版", "description": "结算、换产物 —— 顺序错了会丢数据"},
    {"name": "产物", "description": "class 产物的上传与取回"},
    {"name": "服务配置", "description": "服务的登记与修改（存数据库，改完立即生效）"},
]


def create_app(cfg_path, *, with_watch=False, interval=None):
    @contextlib.asynccontextmanager
    async def lifespan(app):
        cfg = config.load_config(cfg_path)
        url = prepare_database(cfg)
        log("数据库：%s" % config.describe_database_url(url))
        render_dashboard(load_runtime(cfg_path))

        collect = cfg.get("collect") or {}
        if collect.get("port"):
            collector = PushCollector(lambda: load_runtime(cfg_path))
            set_collector(collector)
            collector.start(int(collect["port"]), collect.get("bindAddress", "0.0.0.0"))
            if not with_watch:
                log("  ! 收集端已起，但没带 --with-watch —— 连上来的实例不会被定时取数")

        stop = threading.Event()
        if with_watch:
            every = interval or (cfg.get("watch") or {}).get("intervalSeconds", 300)
            threading.Thread(target=watch_loop, args=(cfg_path, every, stop),
                             daemon=True, name="covhub-watch").start()
            log("采集线程已启动，每 %d 秒轮询一次" % every)

        port = app.state.port
        log("covhub %s 已启动： http://127.0.0.1:%d/  （根目录 %s）"
            % (__version__, port, cfg["dataDir"]))
        log("控制 API： http://127.0.0.1:%d/api/health%s"
            % (port, "" if config.token(cfg) else
               "    [未设置 serve.token：写接口与 data/ 整个目录都对外敞开]"))
        try:
            yield
        finally:
            stop.set()
            if get_collector() is not None:
                get_collector().stop()
                set_collector(None)
            engine.dispose()

    app = FastAPI(
        title="covhub 控制 API", version=__version__, description=DESCRIPTION,
        openapi_tags=TAGS, lifespan=lifespan,
        # 内置的 /docs 从 CDN 拉 Swagger UI，内网起不来，也违背看板不引 CDN 的约定；
        # 文档统一走 /api/openapi.json（免令牌、带 CORS），用外面的 Swagger UI 看
        docs_url=None, redoc_url=None, openapi_url=None,
        default_response_class=PrettyJSONResponse,
    )
    app.state.cfg_path = cfg_path
    app.state.port = 8900
    install_handlers(app)

    @app.middleware("http")
    async def access_log(request, call_next):
        response = await call_next(request)
        # 静态目录不记；health / openapi.json 会被反复轮询，也不记
        path = request.url.path
        if path.startswith("/api/") and path not in OPEN_ROUTES:
            shown = path + ("?" + request.url.query if request.url.query else "")
            log("%s %s -> %d" % (request.method, shown, response.status_code))
        return response

    app.include_router(routes_control.open_router)
    app.include_router(routes_control.router)
    app.include_router(routes_services.router)
    app.include_router(routes_services.import_router)
    app.include_router(static.router)        # 兜底，必须最后挂
    return app


def serve(cfg_path, port, with_watch=False, interval=None):
    """起 uvicorn。传 app **对象**而不是 import 字符串：uvicorn 对对象拒绝 workers>1
    和 reload，从物理上堵死「多进程」这条路。"""
    import uvicorn

    app = create_app(cfg_path, with_watch=with_watch, interval=interval)
    app.state.port = port
    uvicorn.run(app, host="0.0.0.0", port=port, workers=1, access_log=False,
                log_level="warning")
