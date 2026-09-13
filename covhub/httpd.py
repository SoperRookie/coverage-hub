"""旧的 http.server 控制面（阶段④换成 FastAPI 后删除）。"""

import http.server
import json
import os
import secrets
import socketserver
import tempfile
import threading
import urllib.parse

from . import __version__, ops
from .artifacts import pack_classes, store_classes
from .collector import PushCollector, set_collector
from .config import find_service, load_config, token as _token
from .runtime import load_runtime
from .dashboard import render_dashboard
from .errors import CovhubError
from .locks import LOCK
from .logbuf import capture_logs, log
from .openapi_spec import OPENAPI_SPEC
from .db import repo
from .watch import watch_loop

# --------------------------------------------------------------------------
# 远程控制 API
#
# 整套方案只需要一个服务端。被测服务所在的机器和发版节点不装 Python、不装 java、
# 不放配置文件，全部通过这些接口驱动 hub 干活 —— 它们只需要 curl。
#
#   GET  /api/health                            存活探测，不需要令牌
#   GET  /api/openapi.json                      接口的 OpenAPI 描述，不需要令牌
#   GET  /api/status[?service=X]                连通性与最新覆盖率（JSON）
#   GET  /api/agent-opts?service=X              应注入的 -javaagent 参数串
#   GET  /api/agent.jar                         下载 jacocoagent.jar
#   GET  /api/diagnose?service=X[&version=V]    诊断 exec 与 class 是否对得上
#   POST /api/dump?service=X                    拉一次快照（累加）
#   POST /api/predeploy?service=X&version=V     结算并归档，停服前调用
#         &allowMissing=1                       目标已离线时不报错
#   POST /api/report?service=X                  用已有 exec 重出报告
#   POST /api/retarget?service=X&version=V      更新 version / classfiles
#         &classfiles=/a,/b
#   POST /api/upload-classes?service=X          上传该版本的 class 产物压缩包
#         &version=V[&retarget=1]               （tar.gz / zip，正文为二进制）
#   GET  /api/classes?service=X&version=V       把该版本的 class 产物打成 tar.gz 回传
#
# 参数可用 query string，也可用 JSON body。配置了 serve.token（或设了环境变量
# COVHUB_TOKEN）时，除 /api/health 外都要带 X-Covhub-Token 头或 ?token=。
# --------------------------------------------------------------------------
OPEN_ROUTES = ("/api/health", "/api/openapi.json")


# 浏览器里点开报告时带令牌用的 Cookie。报告页里全是相对链接，不可能每条都
# 挂上 ?token=，所以带对一次就种下它，后续静态请求靠它放行。
TOKEN_COOKIE = "covhub_token"


def _token_ok(given, expected):
    """令牌比对。用 compare_digest 而不是 == —— 逐字符短路会泄漏正确的前缀长度。

    比的是 bytes：token 里出现非 ASCII 时 compare_digest 的 str 形式会直接抛错。
    """
    return secrets.compare_digest(str(given or "").encode("utf-8"),
                                  str(expected or "").encode("utf-8"))


def _as_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [p.strip() for p in str(value).split(",") if p.strip()]


def _capture(fn, *a, **kw):
    """在锁内执行操作，把它打印的日志一起回给调用方。

    业务失败（CovhubError）翻译成 409 —— 让流水线那边非零退出，而不是拿到
    一个"成功"的空响应继续往下走。SystemExit 也按 409 兜底，但库函数不该抛它。
    """
    with LOCK, capture_logs() as lines:
        try:
            fn(*a, **kw)
            code = 200
        except CovhubError as exc:
            lines.append("[covhub] %s\n" % exc)
            code = 409
        except SystemExit as exc:
            code = 409 if exc.code else 200
        except Exception as exc:
            lines.append("[covhub] 错误：%s\n" % exc)
            code = 500
    return code, "".join(lines)


def api_dispatch(cfg_path, method, route, params):
    """返回 (状态码, JSON 可序列化对象)。配置每次重读，retarget 后立即生效。"""
    cfg = load_runtime(cfg_path)

    if route == "/api/health":
        return 200, {"ok": True, "version": __version__,
                     "services": [s["name"] for s in cfg.get("services", [])]}

    if route == "/api/openapi.json":
        return 200, OPENAPI_SPEC

    if route == "/api/status" and method == "GET":
        name = params.get("service")
        if name and not any(s["name"] == name for s in cfg.get("services", [])):
            return 404, {"ok": False, "error": "配置里没有名为 %r 的服务" % name}
        return 200, {"ok": True, "services": ops.status(cfg, name)}

    name = params.get("service")
    if not name:
        return 400, {"ok": False, "error": "缺少参数 service"}
    if not any(s["name"] == name for s in cfg.get("services", [])):
        return 404, {"ok": False, "error": "配置里没有名为 %r 的服务" % name}
    find_service(cfg, name)

    if route == "/api/agent-opts":
        if method != "GET":
            return 405, {"ok": False, "error": "/api/agent-opts 只接受 GET"}
        return 200, {"ok": True, "service": name, "agentOpts": ops.agent_opts(cfg, name)}

    if route == "/api/diagnose":
        if method != "GET":
            return 405, {"ok": False, "error": "/api/diagnose 只接受 GET"}
        try:
            with LOCK:
                return 200, {"ok": True, "diagnose": ops.diagnose(cfg, name, params.get("version"))}
        except Exception as exc:
            return 500, {"ok": False, "error": str(exc)}

    if method != "POST":
        return 405, {"ok": False, "error": "%s 只接受 POST" % route}

    kw = {}
    if route == "/api/dump":
        fn = ops.dump
    elif route == "/api/report":
        fn = ops.report
    elif route == "/api/predeploy":
        fn = ops.predeploy
        kw = {"version": params.get("version"),
              "allow_missing": str(params.get("allowMissing", "")).lower() in ("1", "true", "yes")}
    elif route == "/api/retarget":
        fn = ops.retarget
        kw = {"version": params.get("version"),
              "classfiles": _as_list(params.get("classfiles")),
              "sourcefiles": _as_list(params.get("sourcefiles"))}
    else:
        return 404, {"ok": False, "error": "未知接口 " + route}

    code, output = _capture(fn, cfg, name, **kw)
    body = {"ok": code == 200, "service": name, "log": output}
    if code == 200:
        body["latest"] = repo.latest(name)
    return code, body


def cmd_serve(cfg, args):
    port = args.port or cfg.get("serve", {}).get("port", 8900)
    root = cfg["dataDir"]
    cfg_path = args.config
    os.makedirs(root, exist_ok=True)
    render_dashboard(cfg)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=root, **kw)

        def log_message(self, fmt, *a):
            pass

        def do_GET(self):
            route = self._route()
            if route.startswith("/api/") or route == "/agent.jar":
                return self._api("GET")
            return self._static(super().do_GET)

        def do_HEAD(self):
            route = self._route()
            if route.startswith("/api/") or route == "/agent.jar":
                return self._send(405, {"ok": False, "error": "该接口不支持 HEAD"})
            return self._static(super().do_HEAD)

        def do_POST(self):
            return self._api("POST")

        # ---- 以下是控制 API ----

        def _route(self):
            path = urllib.parse.urlsplit(self.path).path
            return path.rstrip("/") or "/"

        def _params(self):
            query = urllib.parse.urlsplit(self.path).query
            params = {k: v[-1] for k, v in urllib.parse.parse_qs(query).items()}
            length = int(self.headers.get("Content-Length") or 0)
            # 上传接口的正文是二进制压缩包，留给 _upload 自己读，这里绝不能碰
            if length and self._route() != "/api/upload-classes":
                raw = self.rfile.read(length).decode("utf-8", "replace").strip()
                if raw.startswith("{"):
                    try:
                        params.update(json.loads(raw))
                    except ValueError:
                        pass
                elif raw:
                    params.update({k: v[-1] for k, v in urllib.parse.parse_qs(raw).items()})
            return params

        def _cookie_token(self):
            raw = self.headers.get("Cookie") or ""
            for part in raw.split(";"):
                key, _, value = part.strip().partition("=")
                if key == TOKEN_COOKIE:
                    return urllib.parse.unquote(value)
            return ""

        def _authorized(self, current, params):
            expected = _token(current)
            if not expected or self._route() in OPEN_ROUTES:
                return True
            given = (self.headers.get("X-Covhub-Token") or params.get("token")
                     or self._cookie_token() or "")
            return _token_ok(given, expected)

        def _grant(self, expected):
            """令牌带对了：种上 Cookie，再跳回不带令牌的同一地址。

            令牌留在地址栏会被浏览器历史和 Referer 一起带走，所以只让它在
            这一次请求里出现。
            """
            parts = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parts.query)
            query.pop("token", None)
            target = urllib.parse.urlunsplit(
                ("", "", parts.path, urllib.parse.urlencode(query, doseq=True), "")) or "/"
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Set-Cookie", "%s=%s; Path=/; HttpOnly; SameSite=Strict"
                             % (TOKEN_COOKIE, urllib.parse.quote(expected)))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _static(self, serve):
            """dataDir 的静态服务 —— 配了令牌就必须和 /api/ 一起拦。

            这底下不只有报告：artifacts/ 是线上跑的那份字节码（反编译即源码），
            exec/ 是不可再生的执行轨迹，state.json 有全部历史。只护住 /api/
            而把整棵树敞开，等于那道门白装。
            """
            try:
                current = load_config(cfg_path)
            except CovhubError:
                return self._send(500, {"ok": False, "error": "配置文件读取失败"})
            expected = _token(current)
            if not expected:
                return serve()

            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            from_query = query.get("token", [""])[-1]
            if from_query and _token_ok(from_query, expected):
                return self._grant(expected)
            given = (self.headers.get("X-Covhub-Token")
                     or self._cookie_token() or from_query)
            if not _token_ok(given, expected):
                return self._send(401, {
                    "ok": False,
                    "error": "令牌无效或缺失",
                    "hint": "浏览器：在地址后加 ?token=<serve.token>，之后靠 Cookie 放行；"
                            "命令行：带 X-Covhub-Token 头",
                })
            return serve()

        def _send(self, code, payload, ctype="application/json; charset=utf-8",
                  headers=None):
            if isinstance(payload, bytes):
                data = payload
            else:
                data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _upload(self, current, params):
            """接收 class 产物压缩包（tar.gz / zip），可选顺手 retarget。

            正文是二进制，不能走 _params()，所以这里单独读 —— 大包直接落盘，
            不整个读进内存。
            """
            name, version = params.get("service"), params.get("version")
            if not name or not version:
                return self._send(400, {"ok": False, "error": "需要参数 service 与 version"})
            if not any(s["name"] == name for s in current.get("services", [])):
                return self._send(404, {"ok": False, "error": "配置里没有名为 %r 的服务" % name})
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return self._send(400, {"ok": False, "error": "请求体为空，用 --data-binary 上传压缩包"})

            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".upload")
            try:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    tmp.write(chunk)
                    remaining -= len(chunk)
                tmp.close()
                svc = find_service(current, name)
                with LOCK:
                    dest, count = store_classes(current, svc, version, tmp.name)
            except Exception as exc:
                return self._send(400, {"ok": False, "error": str(exc)})
            finally:
                os.unlink(tmp.name)

            body = {"ok": True, "service": name, "version": version,
                    "path": dest, "classes": count}
            # retarget=1：上传完直接把配置指向这份产物，省一次调用
            if str(params.get("retarget", "")).lower() in ("1", "true", "yes"):
                code, out = _capture(ops.retarget, current, name, version=version,
                                     classfiles=[dest])
                body["retarget"] = out
                if code != 200:
                    body["ok"] = False
                    return self._send(code, body)
            self._send(200, body)

        def _download_classes(self, current, params):
            """把某个版本的 class 产物打包回传。

            推 Sonar 需要 -Dsonar.java.binaries 指向**采集时运行的那份 class**，
            有了这个接口，发版节点不必自己囤一份历史产物。
            """
            name, version = params.get("service"), params.get("version")
            if not name or not version:
                return self._send(400, {"ok": False, "error": "需要参数 service 与 version"})
            if not any(s["name"] == name for s in current.get("services", [])):
                return self._send(404, {"ok": False, "error": "配置里没有名为 %r 的服务" % name})

            svc = find_service(current, name)
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz")
            tmp.close()
            try:
                with LOCK:
                    count, size = pack_classes(current, svc, version, tmp.name)
                log("%s：回传 %s 的 class 产物 %d 个（%.1f MB）"
                    % (name, version, count, size / 1048576.0))
                with open(tmp.name, "rb") as f:
                    data = f.read()
            except RuntimeError as exc:
                return self._send(404, {"ok": False, "error": str(exc)})
            except Exception as exc:
                return self._send(500, {"ok": False, "error": str(exc)})
            finally:
                os.unlink(tmp.name)

            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition",
                             'attachment; filename="classes-%s-%s.tar.gz"' % (name, version))
            self.send_header("X-Covhub-Classes", str(count))
            self.end_headers()
            self.wfile.write(data)

        def _api(self, method):
            route = self._route()
            try:
                params = self._params()
                current = load_runtime(cfg_path)
            except CovhubError:
                return self._send(500, {"ok": False, "error": "配置文件读取失败"})
            if not self._authorized(current, params):
                return self._send(401, {"ok": False, "error": "令牌无效或缺失"})

            # agent jar 直接从 hub 下载：被测机器不必预先铺一份，
            # 容器的 initContainer 一条 curl 就能拿到。
            if route in ("/api/agent.jar", "/agent.jar"):
                jar = current["jacocoAgent"]
                if not os.path.isfile(jar):
                    return self._send(404, {"ok": False, "error": "找不到 " + jar})
                with open(jar, "rb") as f:
                    return self._send(200, f.read(), "application/java-archive")

            if route == "/api/upload-classes":
                return self._upload(current, params)

            if route == "/api/classes":
                return self._download_classes(current, params)

            code, body = api_dispatch(cfg_path, method, route, params)
            if route not in OPEN_ROUTES:
                log("%s %s -> %d" % (method, self.path, code))

            # 纯文本模式，方便 shell 里直接 $(curl ...) 取参数串
            if code == 200 and params.get("format") == "text" and "agentOpts" in body:
                return self._send(200, (body["agentOpts"] + "\n").encode("utf-8"),
                                  "text/plain; charset=utf-8")

            if route == "/api/openapi.json":
                # Swagger UI / 网关几乎总在别的地址上，没有这个头浏览器会把拉取
                # 请求拦掉，文档就等于接不进去。这一条本来就免令牌、不含部署信息，
                # 放开跨源读取没有额外代价。
                #
                # **只给这一条。** 带令牌的接口不能开 —— 鉴权认 Cookie，给它们加
                # CORS 等于让任意页面替已登录的浏览器调写接口。
                return self._send(code, body,
                                  headers={"Access-Control-Allow-Origin": "*"})
            self._send(code, body)

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    collect = cfg.get("collect") or {}
    if collect.get("port"):
        collector = PushCollector(lambda: load_runtime(cfg_path))
        set_collector(collector)
        collector.start(int(collect["port"]), collect.get("bindAddress", "0.0.0.0"))
        if not getattr(args, "with_watch", False):
            log("  ! 收集端已起，但没带 --with-watch —— 连上来的实例不会被定时取数")

    if getattr(args, "with_watch", False):
        interval = args.interval or cfg.get("watch", {}).get("intervalSeconds", 300)
        threading.Thread(target=watch_loop, args=(cfg_path, interval), daemon=True).start()
        log("采集线程已启动，每 %d 秒轮询一次" % interval)

    log("covhub %s 已启动： http://127.0.0.1:%d/  （根目录 %s）" % (__version__, port, root))
    log("控制 API： http://127.0.0.1:%d/api/health%s"
        % (port, "" if _token(cfg) else
           "    [未设置 serve.token：写接口与 data/ 整个目录都对外敞开]"))
    with Server(("0.0.0.0", port), Handler) as httpd:
        httpd.serve_forever()
