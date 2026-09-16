"""服务配置的校验模型。CLI 的 service add、import、HTTP 的 /api/services 共用。

字段名直接用配置文件里的 camelCase，不做别名转换 —— 一份配置在 YAML、API、
`service show` 三处长得一样。extra="forbid"：拼错键名静默失效正是过去 YAML
最坑人的地方，入库就该把这条堵上。
"""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 服务名是目录名、URL 段、push 通道的 sessionid 前缀，字符集要保守
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# 项目名只是显示与 URL 参数（会编码），允许中文：\w 在 Python 里含 CJK；仍不能以 . 或 - 开头
PROJECT_RE = re.compile(r"^[\w][\w.-]*$")
# 这些名字在 hub 的 URL 根下另有含义（控制 API、接口文档、自托管时的前端产物），
# 服务不能叫这些 —— 路由先匹配，重名的服务报告目录会被静默遮住
RESERVED_NAMES = {"api", "assets", "index.html", "favicon.ico", "agent.jar",
                  "docs", "swagger"}


def _as_str_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    return [str(p) for p in value]


class ServiceSpec(BaseModel):
    """一条完整的服务配置。

    bindAddress / dumpRetry / version 故意不给默认值：agent_opts 等处用的是
    svc.get("bindAddress", "0.0.0.0") 这种「键不存在才取默认」的写法，入库时
    填了默认值反而会改变 sessionid 之类的行为。
    """
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=100)
    project: str | None = Field(default=None, max_length=100, description="所属项目名")
    version: str | None = Field(default=None, max_length=100)
    channel: str = "pull"
    address: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    bindAddress: str | None = Field(default=None, max_length=255)
    includes: list[str] = []
    excludes: list[str] = []
    classDumpDir: str | None = Field(default=None, max_length=500)
    classfiles: list[str] = []
    sourcefiles: list[str] = []
    reportExcludes: list[str] = []
    sourceEncoding: str | None = Field(default=None, max_length=32)
    dumpRetry: int | None = Field(default=None, ge=0)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        if v is not None and not NAME_RE.match(v):
            raise ValueError("服务名只能用字母、数字、. _ -，且不能以 . 或 - 开头")
        if v is not None and v.lower() in RESERVED_NAMES:
            raise ValueError("%r 是保留名，不能用作服务名" % v)
        return v

    @field_validator("project")
    @classmethod
    def _project(cls, v):
        if v is not None and not PROJECT_RE.match(v):
            raise ValueError("项目名只能用字母（含中文）、数字、. _ -，且不能以 . 或 - 开头")
        return v

    @field_validator("version", mode="before")
    @classmethod
    def _version(cls, v):
        # YAML 里裸写的 1.4 会被读成数字，这里统一收成字符串，别让它带着 float 进库
        return None if v is None else str(v)

    @field_validator("channel")
    @classmethod
    def _channel(cls, v):
        v = (v or "pull").lower()
        if v not in ("pull", "push"):
            raise ValueError("channel 只能是 pull 或 push")
        return v

    @field_validator("includes", "excludes", "classfiles", "sourcefiles", "reportExcludes",
                     mode="before")
    @classmethod
    def _lists(cls, v):
        return _as_str_list(v)

    @model_validator(mode="after")
    def _pull_needs_endpoint(self):
        if self.channel == "pull" and (not self.address or not self.port):
            raise ValueError("pull 通道的服务必须配置 address 和 port（hub 要连过去拉数据）")
        return self

    def to_fields(self):
        """入库用：None 的标量不带，列表恒带。"""
        return self.model_dump(exclude_none=True)


class ServicePatch(BaseModel):
    """局部更新：只校验给到的字段。"""
    model_config = ConfigDict(extra="forbid")

    project: str | None = Field(default=None, max_length=100)   # 显式给 null 表示解绑
    version: str | None = Field(default=None, max_length=100)
    channel: str | None = None
    address: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    bindAddress: str | None = Field(default=None, max_length=255)
    includes: list[str] | None = None
    excludes: list[str] | None = None
    classDumpDir: str | None = Field(default=None, max_length=500)
    classfiles: list[str] | None = None
    sourcefiles: list[str] | None = None
    reportExcludes: list[str] | None = None
    sourceEncoding: str | None = Field(default=None, max_length=32)
    dumpRetry: int | None = Field(default=None, ge=0)

    _version = field_validator("version", mode="before")(ServiceSpec._version.__func__)
    _channel = field_validator("channel")(lambda cls, v: None if v is None
                                          else ServiceSpec._channel.__func__(cls, v))
    _lists = field_validator("includes", "excludes", "classfiles", "sourcefiles",
                             "reportExcludes", mode="before")(
        lambda cls, v: None if v is None else _as_str_list(v))

    _project = field_validator("project")(lambda cls, v: None if v is None
                                          else ServiceSpec._project.__func__(cls, v))

    def to_fields(self):
        return self.model_dump(exclude_unset=True)


class ProjectSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=100)
    title: str | None = Field(default=None, max_length=200)
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        if not PROJECT_RE.match(v):
            raise ValueError("项目名只能用字母（含中文）、数字、. _ -，且不能以 . 或 - 开头")
        return v


class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    description: str | None = None
