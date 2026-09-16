"""hub 级配置：读取、探测、相对路径规整、数据库地址。

服务配置（services）不在这里 —— 它们在数据库里，由 runtime.load_runtime()
填进 cfg["services"]。配置文件只剩 hub 自己的几项。
"""

import json
import os

from .errors import ConfigError, ServiceNotFound
from .logbuf import log

# 新名字在前，旧名字继续认：已有部署的 targets.yaml 不用改名也能跑
CONFIG_CANDIDATES = ("covhub.yaml", "covhub.yml", "covhub.json",
                     "targets.yaml", "targets.yml", "targets.json")


def config_format(path):
    """按扩展名判断配置格式，.yaml / .yml 走 YAML，其余按 JSON。"""
    return "yaml" if os.path.splitext(path)[1].lower() in (".yaml", ".yml") else "json"


def resolve_config_path(explicit):
    """-c 没给时按 CONFIG_CANDIDATES 顺序探测。

    两种格式长期并存：已有部署的 JSON 原样能跑，新机器默认用 YAML
    （能写注释、不用数逗号）。都不存在时返回推荐的那个，让报错指向 YAML。
    """
    if explicit:
        return explicit
    for name in CONFIG_CANDIDATES:
        if os.path.isfile(name):
            return name
    return CONFIG_CANDIDATES[0]


def read_config_file(path):
    """读配置原文并解析成 dict，不做路径规整（retarget 也用它做匹配）。"""
    if not os.path.isfile(path):
        raise ConfigError("找不到配置文件 %s，先运行 covhub.py init 生成模板" % path)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if config_format(path) == "yaml":
        data = parse_yaml(text, path)
    else:
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ConfigError("配置文件 %s 不是合法 JSON：%s" % (path, exc))
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("配置文件 %s 的顶层必须是对象" % path)
    return data


def parse_yaml(text, path):
    """YAML 解析依赖 PyYAML。

    工具其余部分只用标准库，YAML 是唯一的例外 —— 装不了第三方包的机器
    （离线内网、老镜像）可以继续用 JSON，两种格式功能完全等价。
    """
    try:
        import yaml
    except ImportError:
        raise ConfigError("解析 %s 需要 PyYAML：pip install PyYAML\n"
            "        装不上的话可以用 JSON 配置：covhub.py init --json" % path)
    try:
        return yaml.safe_load(text)
    except Exception as exc:
        raise ConfigError("配置文件 %s 解析失败：%s" % (path, exc))


def load_config(path):
    cfg = read_config_file(path)
    base = os.path.dirname(os.path.abspath(path))
    # 相对路径一律相对配置文件所在目录解析，便于整个目录搬迁。
    # 目录与文件路径记进 cfg：retarget 要回写文件，服务入库后相对路径也按这里展开。
    cfg["baseDir"] = base
    cfg["configPath"] = os.path.abspath(path)
    for key in ("jacocoCli", "jacocoAgent", "dataDir"):
        if cfg.get(key) and not os.path.isabs(cfg[key]):
            cfg[key] = os.path.normpath(os.path.join(base, cfg[key]))
    # serve.webDir 同样相对配置文件解析 —— 它是磁盘路径，跟着进程 CWD 走的话
    # systemd 起的 hub 和手敲命令起的 hub 会找到不同的目录
    serve = cfg.get("serve")
    if isinstance(serve, dict) and serve.get("webDir") and not os.path.isabs(serve["webDir"]):
        serve["webDir"] = os.path.normpath(os.path.join(base, serve["webDir"]))
    # 文件里的 services 早已不生效。不报错（升级第一步就把人卡住太粗暴），
    # 但每个进程提醒一次，直到有人跑过 import 并把这一段删掉
    if cfg.get("services") and not _warned.get(cfg["configPath"]):
        _warned[cfg["configPath"]] = True
        log("! %s 里的 services 已不再生效，服务改由数据库管理：先 covhub import，"
            "再删掉这一段" % path)
    cfg["services"] = []
    return cfg


_warned = {}


def resolve_database_url(cfg):
    """环境变量 COVHUB_DATABASE_URL > database.url > 配置文件旁的 SQLite。

    默认库放在配置文件旁边而**不是 dataDir 里**：dataDir 是看板的静态目录，
    放进去等于把整个库开放下载。
    """
    url = os.environ.get("COVHUB_DATABASE_URL") or (cfg.get("database") or {}).get("url")
    if not url:
        url = "sqlite:///" + os.path.join(cfg["baseDir"], "covhub.db").replace("\\", "/")
    return url


def database_auto_upgrade(cfg):
    value = (cfg.get("database") or {}).get("autoUpgrade", True)
    return str(value).lower() not in ("0", "false", "no", "off")


def describe_database_url(url):
    """启动日志用：方言 + 主机，密码打码。生产忘配环境变量静默跑在 SQLite 上是常见事故。"""
    if url.startswith("sqlite"):
        return url
    head, _, rest = url.partition("://")
    if "@" in rest:
        creds, _, hostpart = rest.rpartition("@")
        user = creds.split(":", 1)[0]
        return "%s://%s:***@%s" % (head, user, hostpart)
    return url


def find_service(cfg, name):
    for svc in cfg.get("services", []):
        if svc["name"] == name:
            return svc
    raise ServiceNotFound("配置里没有名为 %r 的服务，已定义：%s"
        % (name, ", ".join(s["name"] for s in cfg.get("services", []))))

def token(cfg):
    """控制面令牌：环境变量 COVHUB_TOKEN 优先于配置文件的 serve.token。"""
    return os.environ.get("COVHUB_TOKEN") or (cfg.get("serve") or {}).get("token") or ""


CONFIG_TEMPLATE_YAML = """\
# covhub 配置（hub 自己的几项）。相对路径一律相对本文件所在目录解析。
# 服务配置不在这里 —— 它们在数据库里，用 covhub service add 登记，
# 或者 covhub import 从旧的 targets.yaml 一次性导入。
jacocoAgent: ./lib/jacocoagent.jar   # 被测端能看到的路径，容器场景写容器内路径
jacocoCli: ./lib/jacococli.jar
dataDir: ./data

database:
  # 环境变量 COVHUB_DATABASE_URL 优先于这里。留空则用本文件旁边的 SQLite
  # （covhub.db，只适合单机试用）。MySQL 8 写法：
  #   mysql+pymysql://covhub:密码@主机:3306/covhub?charset=utf8mb4
  url: ""
  autoUpgrade: true          # 启动时自动把表结构升到最新；关掉则结构不对时拒绝启动

serve:
  port: 8900
  token: ""                  # 控制 API 的令牌，不配则任何人都能调写接口
  webDir: ""                 # 看板前端产物目录。留空 = 前后端分离部署，看板由 nginx 之类
                             # 托管，hub 只做 API + 报告目录；填上则由 hub 一起托管（单机够用）

watch:
  intervalSeconds: 300       # 轮询间隔，同时是断代时数据丢失的上界

collect:                     # push 通道的收集端，只有配了 port，serve 才会起它
  port: 6400
  bindAddress: 0.0.0.0
  advertiseAddress: 改成被测端能访问到的 hub 地址
  dumpTimeoutSeconds: 20     # 向单个实例取数的上限，卡住的实例等这么久就丢弃
"""

CONFIG_TEMPLATE_JSON = {
    "jacocoAgent": "./lib/jacocoagent.jar",
    "jacocoCli": "./lib/jacococli.jar",
    "dataDir": "./data",
    "database": {"url": "", "autoUpgrade": True},
    "serve": {"port": 8900, "token": "", "webDir": ""},
    "watch": {"intervalSeconds": 300},
    "collect": {"port": 6400, "bindAddress": "0.0.0.0",
                "advertiseAddress": "改成被测端能访问到的 hub 地址",
                "dumpTimeoutSeconds": 20},
}

# service add --from-file 用的模板
SERVICE_TEMPLATE_YAML = """\
# 一条服务配置，用 covhub service add <name> --from-file 本文件 登记进数据库。
# 字段含义见 README「配置」一节。
name: example-service
project: example             # 可选，所属项目（先 covhub project add example）
version: "1.0.0"             # 版本号一律加引号，裸写的 1.4 会被读成数字
channel: pull                # pull：hub 去连 agent；push：agent 连回 hub
address: 127.0.0.1           # pull：agent 所在机器，hub 连过去拉数据
port: 6300
bindAddress: 0.0.0.0         # pull：agent 在被测端监听的地址
includes:                    # 传给 agent，决定是否插桩，改了要重启服务
  - com.example.*
excludes: []
classDumpDir: /tmp/covhub-classes/example-service   # 被测端路径
classfiles:                  # 出报告用的 class（hub 上的路径），必须与运行中的服务同一份产物
  - ./data/example-service/artifacts/1.0.0
sourcefiles: []              # 可选，配了才能在报告里下钻到源码行
reportExcludes:              # 只影响报告口径，随时可改重出报告
  - com/example/**/dto/**
sourceEncoding: UTF-8
"""
