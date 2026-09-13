"""hub 级配置：读取、探测、相对路径规整。"""

import json
import os

from .errors import ConfigError, ServiceNotFound

CONFIG_CANDIDATES = ("targets.yaml", "targets.yml", "targets.json")


def config_format(path):
    """按扩展名判断配置格式，.yaml / .yml 走 YAML，其余按 JSON。"""
    return "yaml" if os.path.splitext(path)[1].lower() in (".yaml", ".yml") else "json"


def resolve_config_path(explicit):
    """-c 没给时按 targets.yaml → targets.yml → targets.json 顺序探测。

    两种格式长期并存：已有部署的 targets.json 原样能跑，新机器默认用 YAML
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
    for svc in cfg.get("services", []):
        for key in ("classfiles", "sourcefiles"):
            svc[key] = [p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))
                        for p in svc.get(key, [])]
    return cfg


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
# covhub 配置。相对路径一律相对本文件所在目录解析。
jacocoAgent: ./lib/jacocoagent.jar
jacocoCli: ./lib/jacococli.jar
dataDir: ./data

serve:
  port: 8900
  token: ""                  # 控制 API 的令牌，不配则任何人都能调写接口

watch:
  intervalSeconds: 300       # 轮询间隔，同时是断代时数据丢失的上界

collect:                     # push 通道的收集端，只有配了 port，serve 才会起它
  port: 6400
  bindAddress: 0.0.0.0
  advertiseAddress: 改成被测端能访问到的 hub 地址
  dumpTimeoutSeconds: 20     # 向单个实例取数的上限，卡住的实例等这么久就丢弃

services:
  - name: example-service
    version: "1.0.0"
    channel: pull            # pull：hub 去连 agent；push：agent 连回 hub
    address: 127.0.0.1       # agent 所在机器，hub 连过去拉数据
    port: 6300
    bindAddress: 0.0.0.0     # agent 在被测端监听的地址
    includes:                # 传给 agent，决定是否插桩，改了要重启服务
      - com.example.*
    excludes: []
    classDumpDir: /tmp/covhub-classes/example-service   # 被测端路径
    classfiles:              # 出报告用的 class，必须与运行中的服务同一份产物
      - /path/to/classes
    sourcefiles:             # 可选，配了才能在报告里下钻到源码行
      - /path/to/src/main/java
    reportExcludes:          # 只影响报告口径，随时可改重出报告
      - com/example/**/dto/**
    sourceEncoding: UTF-8
"""
