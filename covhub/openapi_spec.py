"""手写的 OpenAPI 描述（阶段④由 FastAPI 生成后删除）。"""

from . import __version__

# --------------------------------------------------------------------------
# OpenAPI 文档
#
# 控制面接口的机器可读描述，GET /api/openapi.json 取。和 /api/health 一样**不要
# 令牌** —— 它是静态结构，连服务名都不带，网关、Swagger UI、Apifox 才好直接拉。
#
# 这份 dict 是唯一事实来源，仓库里的 docs/openapi.json 是它的导出产物；
# 改了接口要一并重新导出，命令见 CLAUDE.md 的验证流程。
# --------------------------------------------------------------------------

def _oa_schema(name):
    return {"$ref": "#/components/schemas/" + name}


def _oa_body(name, desc):
    return {"description": desc,
            "content": {"application/json": {"schema": _oa_schema(name)}}}


def _oa_param(name, desc, required=True, example=None):
    p = {"name": name, "in": "query", "required": required,
         "description": desc, "schema": {"type": "string"}}
    if example is not None:
        p["schema"]["example"] = example
    return p


_OA_ERR = {
    "400": _oa_body("Error", "缺少必需参数"),
    "401": _oa_body("Error", "令牌无效或缺失（hub 配了 serve.token 时）"),
    "404": _oa_body("Error", "配置里没有这个服务"),
    "405": _oa_body("Error", "方法不对，见各接口标注"),
    "500": _oa_body("Error", "hub 内部错误"),
}


def _oa_write_responses(extra_404=None):
    """写接口的公共响应。

    409 是这套 API 最要紧的一个约定：它表示**业务上失败了**（典型如 predeploy
    时目标已经离线），调用方必须据此让部署流程停下来，而不是拿着一个 2xx 继续
    往下走 —— 那会静默丢掉一整段不可再生的覆盖率数据。
    """
    out = {
        "200": _oa_body("CommandResult", "执行成功。log 是这次执行打印的日志"),
        "409": _oa_body("CommandResult",
                        "业务失败（目标不可达、数据不满足前提等）。"
                        "部署流程应当就此停下"),
    }
    out.update(_OA_ERR)
    if extra_404:
        out["404"] = _oa_body("Error", extra_404)
    return out


OPENAPI_SPEC = {
    "openapi": "3.0.3",
    "info": {
        "title": "covhub 控制 API",
        "version": __version__,
        "description":
            "JaCoCo 运行期覆盖率的采集与看板。整套方案只有 hub 这一个服务端，"
            "被测机器和发版节点不装 Python、不装 java、不放配置文件，"
            "全部通过这些接口驱动 hub 干活 —— 它们只需要 curl。\n\n"
            "**写接口在 hub 内部串行执行**，返回体里带着这次执行的日志。"
            "**非 2xx 一律表示失败**，调用方应据此让部署流程停下来。\n\n"
            "发版节点上更省事的做法是用 integration/covhub-client.sh 包一层。",
    },
    "servers": [{"url": "/", "description": "hub 自身"}],
    "security": [{"tokenHeader": []}, {"tokenQuery": []}],
    "tags": [
        {"name": "探活", "description": "不需要令牌"},
        {"name": "查询", "description": "只读，不改任何状态"},
        {"name": "采集", "description": "拉数据、出报告，会写 data/"},
        {"name": "发版", "description": "结算、换产物 —— 顺序错了会丢数据"},
        {"name": "产物", "description": "class 产物的上传与取回"},
    ],
    "paths": {},          # 见下方 _oa_paths()
    "components": {
        "securitySchemes": {
            "tokenHeader": {
                "type": "apiKey", "in": "header", "name": "X-Covhub-Token",
                "description": "推荐。令牌走请求头而不是 URL，"
                               "免得被 access log 和 ps 输出记下来",
            },
            "tokenQuery": {
                "type": "apiKey", "in": "query", "name": "token",
                "description": "浏览器里访问看板时用；hub 会种一个 Cookie "
                               "再跳回不带令牌的地址",
            },
        },
        "schemas": {},    # 由 _oa_schemas() 填
    },
}


def _oa_schemas():
    """响应体结构。只描述调用方真正会用到的字段，不追求穷尽。"""
    obj = lambda props, **kw: dict({"type": "object", "properties": props}, **kw)
    S, I, N, B = ({"type": "string"}, {"type": "integer"},
                  {"type": "number"}, {"type": "boolean"})
    arr = lambda items: {"type": "array", "items": items}

    summary = obj({
        "at": dict(S, description="采集时刻", example="2026-09-10T18:05:00"),
        "kind": dict(S, description="哪个动作产生的",
                     enum=["dump", "watch", "predeploy", "report", "seal"]),
        "version": dict(S, description="版本标识"),
        "instruction": dict(N, description="指令覆盖率百分比", example=13.5),
        "branch": dict(N, description="分支覆盖率百分比"),
        "covered": dict(I, description="已执行指令数"),
        "total": dict(I, description="指令总数"),
        "classesHit": dict(I, description="被执行到的类数"),
        "classesTotal": dict(I, description="类总数"),
    }, description="一次采集的结果摘要")

    return {
        "Error": obj({
            "ok": dict(B, example=False),
            "error": dict(S, description="给人看的失败原因"),
        }, required=["ok", "error"]),

        "Health": obj({
            "ok": dict(B, example=True),
            "version": dict(S, description="hub 版本", example=__version__),
            "services": arr(S),
        }),

        "Summary": summary,

        "ServiceStatus": obj({
            "name": S,
            "channel": dict(S, enum=["pull", "push"],
                            description="pull：hub 去连 agent；push：agent 连回 hub"),
            "endpoint": dict(S, description="在哪儿取数的一句话描述",
                             example="10.0.1.7:6300"),
            "online": dict(B, description="pull 是端口连得通；push 是当前有实例连着"),
            "unknown": dict(B, description="push 专有：当前进程没有收集端，"
                                           "说不出在线与否。这不等于离线"),
            "instances": arr(obj({"peer": S, "since": S, "last": S}),),
            "version": S,
            "classfiles": arr(dict(S, description="hub 上的路径")),
            "latest": summary,
        }),

        "StatusResponse": obj({
            "ok": dict(B, example=True),
            "services": arr(_oa_schema("ServiceStatus")),
        }),

        "AgentOpts": obj({
            "ok": dict(B, example=True),
            "service": S,
            "agentOpts": dict(S, description="塞进被测服务 JAVA_TOOL_OPTIONS 的参数串",
                              example="-javaagent:/opt/jacoco/jacocoagent.jar="
                                      "output=tcpserver,address=0.0.0.0,port=6300,"
                                      "includes=com.example.*,sessionid=1.4.3"),
        }),

        "Diagnose": obj({
            "service": S,
            "version": S,
            "execFiles": dict(I, description="本周期已有多少个 exec 快照"),
            "classfiles": arr(S),
            "sessions": arr(obj({
                "id": S,
                "start": dict(S, description="被测进程的启动时刻。它变了就说明重启过"),
                "dump": S,
            })),
            "execClasses": dict(I, description="exec 里记录了多少个类"),
            "classFileClasses": dict(I, description="class 产物里有多少个类"),
            "matched": dict(I, description="两边指纹对得上的类数"),
            "matchRate": dict(N, nullable=True,
                              description="匹配率百分比。低于 95 报告就会偏低，"
                                          "低于 50 基本是废的。null 表示 exec 里"
                                          "还没有任何执行记录，不是对不上"),
            "verdict": dict(S, description="给人看的判定"),
            "missingSamples": arr(dict(S, description="exec 里有、class 产物里没有的类")),
            "breaks": arr(obj({
                "at": S,
                "sealedAs": dict(S, description="pull：重启已被自动结算成这个归档"),
                "reason": dict(S, description="push：mixed-versions 表示在线实例"
                                              "跑着两份不同的 class，未结算"),
            })),
        }, description="回答「为什么我的报告是全红的」"),

        "DiagnoseResponse": obj({
            "ok": dict(B, example=True),
            "diagnose": _oa_schema("Diagnose"),
        }),

        "CommandResult": obj({
            "ok": dict(B, description="false 时同时会是非 2xx"),
            "service": S,
            "log": dict(S, description="这次执行打印的日志，原样回给调用方"),
            "latest": summary,
        }),

        "UploadResult": obj({
            "ok": dict(B, example=True),
            "service": S,
            "version": S,
            "path": dict(S, description="产物在 hub 上的落点"),
            "classes": dict(I, description="包里解出多少个 .class。"
                                           "是 0 就说明打包方式不对"),
            "retarget": dict(S, description="带了 retarget=1 时，那一步的日志"),
        }),
    }


OPENAPI_SPEC["components"]["schemas"] = _oa_schemas()


def _oa_paths():
    """11 个接口。GET 一律只读，POST 一律会写 data/ 或配置文件。"""
    svc = _oa_param("service", "服务名，要和配置里的 name 对上", example="order-service")
    svc_opt = _oa_param("service", "服务名。不给则返回全部服务", required=False)
    ver = _oa_param("version", "版本标识", example="1.4.3")

    def op(tag, summary, desc, params, responses, method="get", security=None):
        o = {"tags": [tag], "summary": summary, "description": desc,
             "parameters": params, "responses": responses}
        if security is not None:
            o["security"] = security
        return {method: o}

    return {
        "/api/health": op(
            "探活", "存活探测",
            "唯一不需要令牌的接口。返回 hub 版本和已配置的服务名列表。",
            [], {"200": _oa_body("Health", "hub 活着")},
            security=[]),

        "/api/openapi.json": op(
            "探活", "本文档",
            "这份 OpenAPI 描述自身。同样不需要令牌 —— 它是静态结构，"
            "不含任何部署信息，方便网关和 Swagger UI 直接拉取。",
            [], {"200": {"description": "OpenAPI 3.0 文档",
                         "content": {"application/json": {"schema": {"type": "object"}}}}},
            security=[]),

        "/api/status": op(
            "查询", "连通性与最新覆盖率",
            "看板上那些数字的 JSON 版。push 服务的 unknown=true 表示"
            "当前进程没有收集端，说不出在线与否 —— 那不等于离线。",
            [svc_opt],
            dict(_OA_ERR, **{"200": _oa_body("StatusResponse", "查询成功")})),

        "/api/agent-opts": op(
            "查询", "取该服务应注入的 -javaagent 参数串",
            "被测服务零侵入接入的入口：把返回的参数串塞进 JAVA_TOOL_OPTIONS 即可，"
            "不改代码不改 pom。加 format=text 直接出纯文本，方便 shell 里 $(curl ...)。",
            [svc, _oa_param("format", "填 text 则返回纯文本而不是 JSON",
                            required=False, example="text")],
            dict(_OA_ERR, **{"200": _oa_body("AgentOpts", "参数串")})),

        "/api/agent.jar": op(
            "查询", "下载 jacocoagent.jar",
            "被测机器不必预先铺一份 agent，容器的 initContainer 一条 curl 就能拿到。"
            "读的是配置里 jacocoAgent 指向的文件 —— 那一项填的是**被测端**路径，"
            "两者不一致时这个接口会 404，但 agent-opts 照样输出正确的参数串。",
            [],
            dict(_OA_ERR, **{
                "200": {"description": "jar 文件",
                        "content": {"application/java-archive":
                                    {"schema": {"type": "string", "format": "binary"}}}},
                "404": _oa_body("Error", "配置里 jacocoAgent 指向的文件在 hub 上不存在"),
            })),

        "/api/diagnose": op(
            "查询", "诊断 exec 与 class 产物是否对得上",
            "**任何覆盖率数字不对劲，先跑这个。** 它把 exec 里记录的 class id 和"
            " classfiles 的 class id 求交集 —— 匹配率低就是 class 产物对不上，"
            "这是接入时最贵、最难查、而且**不会报错**的一个坑（报告只会显示全部未覆盖）。",
            [svc, _oa_param("version", "诊断某个已归档版本，不给则诊断当前周期",
                            required=False)],
            dict(_OA_ERR, **{"200": _oa_body("DiagnoseResponse", "诊断结果")})),

        "/api/dump": op(
            "采集", "拉一次快照并出报告",
            "累加，不清零。采完会重新渲染看板。",
            [svc], _oa_write_responses(), method="post"),

        "/api/report": op(
            "采集", "用已有 exec 重出报告",
            "不碰被测服务，只是拿现有数据重新生成一次报告 —— 改了 reportExcludes 之后用。",
            [svc], _oa_write_responses(), method="post"),

        "/api/predeploy": op(
            "发版", "结算并归档（停服前必须调用）",
            "dump --reset + 归档 + 出终版报告。\n\n"
            "**必须在停服之前执行。** 服务一停 agent 随进程消失，那段覆盖率没有任何"
            "补救手段 —— 所以目标不可达时它**故意报 409**，好让部署流程停下来。"
            "确实要跳过就带 allowMissing=1，但那意味着这段数据已经丢了。",
            [svc, _oa_param("version", "版本标识，不给则取配置里的 version",
                            required=False),
             _oa_param("allowMissing", "填 1 则目标已离线时不报错", required=False)],
            _oa_write_responses(), method="post"),

        "/api/retarget": op(
            "发版", "把配置指向新版本的 class 产物",
            "**发版流水线里最容易漏的一步。** JaCoCo 按 CRC64 class id 匹配，"
            "class 产物不跟着版本换，新周期采到的 exec 就和旧 class 对不上，"
            "报告全是未覆盖。用 upload-classes 带 retarget=1 可以省掉这一次调用。\n\n"
            "YAML 配置只替换目标服务的那几行，注释和排版原样保留。",
            [svc, _oa_param("version", "新版本标识", required=False),
             _oa_param("classfiles", "**hub 上**的 class 产物路径，逗号分隔",
                       required=False, example="./data/order-service/artifacts/1.4.3"),
             _oa_param("sourcefiles", "hub 上的源码路径，逗号分隔", required=False)],
            _oa_write_responses(), method="post"),

        "/api/upload-classes": {"post": {
            "tags": ["产物"],
            "summary": "上传该版本的 class 产物压缩包",
            "description":
                "报告是 hub 出的，所以 class 必须在 hub 上，且必须是**线上跑的那一份**。"
                "有了这个接口，被测机器和发版节点都不必和 hub 共享文件系统。\n\n"
                "产物优先用 agent 的 classdumpdir 落盘那份 —— 它和 exec 的指纹"
                "定义上必然匹配，还包含 Spring AOP、MyBatis 代理这类构建产物里"
                "根本没有的动态类。\n\n"
                "包里习惯带的一层顶层目录会自动剥掉；绝对路径和跳出目录的成员一律拒绝。\n\n"
                "带 retarget=1 则上传完顺手把配置的 classfiles 指向这份产物，"
                "省一次 /api/retarget 调用 —— **发版时推荐这么用**。",
            "parameters": [
                svc, ver,
                _oa_param("retarget", "填 1 则上传后把配置指向这份产物",
                          required=False, example="1"),
            ],
            "requestBody": {
                "required": True,
                "description": "压缩包本体，tar.gz 或 zip。curl 用 --data-binary @file",
                "content": {
                    "application/gzip": {"schema": {"type": "string", "format": "binary"}},
                    "application/zip": {"schema": {"type": "string", "format": "binary"}},
                    "application/octet-stream":
                        {"schema": {"type": "string", "format": "binary"}},
                },
            },
            "responses": dict(_OA_ERR, **{
                "200": _oa_body("UploadResult",
                                "已接收。classes 是包里解出的 .class 数量，"
                                "是 0 就说明打包方式不对"),
                "400": _oa_body("Error",
                                "缺参数、请求体为空、或压缩包里有不安全的路径"),
            }),
        }},

        "/api/classes": op(
            "产物", "取回某个版本的 class 产物",
            "打成 tar.gz 回传。推 Sonar 时 -Dsonar.java.binaries 要的就是"
            "**采集时运行的那份 class** —— 有了它，发版节点不必自己囤历史产物。\n\n"
            "两个来源按可信度排序：先找 upload-classes 传上来的 artifacts/<版本>/，"
            "再找结算时 manifest 里记的 classfiles。都没有就 404。",
            [svc, ver],
            dict(_OA_ERR, **{
                "200": {
                    "description": "tar.gz 包。响应头 X-Covhub-Classes 是 .class 数量",
                    "headers": {
                        "X-Covhub-Classes": {"description": "包里的 .class 数量",
                                             "schema": {"type": "integer"}},
                        "Content-Disposition": {"schema": {"type": "string"}},
                    },
                    "content": {"application/gzip":
                                {"schema": {"type": "string", "format": "binary"}}},
                },
                "404": _oa_body("Error",
                                "hub 上没有该版本的产物 —— 发版时没跑过 "
                                "upload-classes，或结算时用的 classfiles 已经不在了"),
            })),
    }


OPENAPI_SPEC["paths"] = _oa_paths()
