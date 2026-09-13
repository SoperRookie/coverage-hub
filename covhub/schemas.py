"""服务配置的校验模型。CLI 的 service add、import、HTTP 的 /api/services 共用。

字段名直接用配置文件里的 camelCase，不做别名转换 —— 一份配置在 YAML、API、
`service show` 三处长得一样。extra="forbid"：拼错键名静默失效正是过去 YAML
最坑人的地方，入库就该把这条堵上。
"""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 服务名是目录名、URL 段、push 通道的 sessionid 前缀，字符集要保守
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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

    def to_fields(self):
        return self.model_dump(exclude_unset=True)
