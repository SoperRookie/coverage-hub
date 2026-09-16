"""接口的请求 / 响应模型。只用来生成 OpenAPI 文档和校验请求体；
响应本身由各路由直接构造（形态与 1.x 保持逐字段兼容）。"""

from pydantic import BaseModel, ConfigDict, Field

from .. import __version__
from ..schemas import ProjectPatch, ProjectSpec, ServicePatch, ServiceSpec  # noqa: F401

__all__ = ["ProjectPatch", "ProjectSpec", "ServicePatch", "ServiceSpec"]


class Error(BaseModel):
    ok: bool = False
    error: str = Field(description="给人看的失败原因")
    hint: str | None = None


class Health(BaseModel):
    ok: bool = True
    version: str = Field(description="hub 版本", examples=[__version__])
    services: list[str] = Field(description="已登记的服务名")


class LoginResult(BaseModel):
    ok: bool = True
    tokenRequired: bool = Field(description="这个 hub 配了 serve.token 吗。false 时不种 Cookie，也不用登录")


class Summary(BaseModel):
    """一次采集的结果摘要。"""
    model_config = ConfigDict(extra="allow")

    id: int | None = Field(default=None, description="快照在数据库里的 id")
    at: str = Field(description="采集时刻", examples=["2026-09-10T18:05:00"])
    kind: str = Field(description="哪个动作产生的：dump / watch / predeploy / report / seal")
    version: str | None = Field(default=None, description="版本标识")
    instruction: float = Field(description="指令覆盖率百分比", examples=[13.5])
    branch: float = Field(description="分支覆盖率百分比")
    covered: int = Field(description="已执行指令数")
    total: int = Field(description="指令总数")
    classesHit: int = Field(description="被执行到的类数")
    classesTotal: int = Field(description="类总数")
    matchRate: float | None = Field(default=None, description="predeploy 时的指纹匹配率")


class Instance(BaseModel):
    peer: str = Field(description="实例的来源地址")
    since: str = Field(description="连入时刻")
    last: str | None = Field(default=None, description="最近一次取数时刻")


class ServiceStatus(BaseModel):
    name: str
    channel: str = Field(description="pull：hub 去连 agent；push：agent 连回 hub")
    endpoint: str = Field(description="在哪儿取数的一句话描述")
    online: bool = Field(description="pull 是端口连得通；push 是当前有实例连着")
    unknown: bool = Field(description="push 专有：当前进程没有收集端，说不出在线与否 —— 那不等于离线")
    instances: list[Instance]
    version: str | None = None
    classfiles: list[str] = Field(description="hub 上的路径")
    latest: Summary | None = None


class StatusResponse(BaseModel):
    ok: bool = True
    services: list[ServiceStatus]


class AgentOpts(BaseModel):
    ok: bool = True
    service: str
    agentOpts: str = Field(
        description="塞进被测服务 JAVA_TOOL_OPTIONS 的参数串",
        examples=["-javaagent:/opt/jacoco/jacocoagent.jar=output=tcpserver,address=0.0.0.0,"
                  "port=6300,includes=com.example.*,sessionid=1.4.2"])


class Session(BaseModel):
    id: str
    start: str = Field(description="被测进程的启动时刻。它变了就说明重启过")
    dump: str


class BreakOut(BaseModel):
    at: str
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    sealedAs: str | None = Field(default=None, description="pull：重启已被自动结算成这个归档")
    reason: str | None = Field(default=None, description="push：mixed-versions 表示在线实例跑着两份不同的 class")
    instances: int | None = None


class Diagnose(BaseModel):
    """回答「为什么我的报告是全红的」。"""
    service: str
    version: str | None = None
    execFiles: int = Field(description="本周期已有多少个 exec 快照")
    sessions: list[Session]
    classfiles: list[str]
    execClasses: int = Field(description="exec 里记录了多少个类")
    classFileClasses: int = Field(description="class 产物里有多少个类")
    matched: int = Field(description="两边指纹对得上的类数")
    matchRate: float | None = Field(
        default=None, description="匹配率百分比。低于 95 报告就会偏低，低于 50 基本是废的；无执行数据时为 null")
    verdict: str = Field(description="给人看的判定")
    missingSamples: list[str] = Field(description="exec 里有、class 产物里没有的类")
    breaks: list[BreakOut]


class DiagnoseResponse(BaseModel):
    ok: bool = True
    diagnose: Diagnose


class CommandResult(BaseModel):
    """写接口的返回体。非 2xx 时 ok=false，log 里有失败原因。"""
    ok: bool = Field(description="false 时同时会是非 2xx")
    service: str
    log: str = Field(description="这次执行打印的日志，原样回给调用方")
    latest: Summary | None = None


class UploadResult(BaseModel):
    ok: bool
    service: str
    version: str
    path: str = Field(description="产物在 hub 上的落点")
    classes: int = Field(description="包里解出多少个 .class。是 0 就说明打包方式不对")
    retarget: str | None = Field(default=None, description="带了 retarget=1 时，那一步的日志")


class ServiceBody(ServiceSpec):
    """PUT 的请求体：name 以路径为准，体里可以不带。其余校验随父类。"""
    name: str | None = Field(default=None, max_length=100)


class ServiceOut(ServiceSpec):
    """库里的一条服务配置（原文，相对路径不展开）。"""
    model_config = ConfigDict(extra="ignore")
    id: int


class ServiceResponse(BaseModel):
    ok: bool = True
    service: ServiceOut


class ServiceListResponse(BaseModel):
    ok: bool = True
    services: list[ServiceOut]


class ImportResult(BaseModel):
    ok: bool = True
    log: str
    services: dict[str, str] = Field(description="每个服务的处理结果：added / updated / skipped / invalid")
    state: dict[str, dict[str, int]] = Field(description="每个服务导入的历史计数")


class ProjectOut(ProjectSpec):
    id: int
    createdAt: str
    updatedAt: str
    services: list[str] = Field(description="项目下的服务名")


class ProjectResponse(BaseModel):
    ok: bool = True
    project: ProjectOut


class ProjectListResponse(BaseModel):
    ok: bool = True
    projects: list[ProjectOut]


class IncrementalBrief(BaseModel):
    covered: int = Field(description="新增行里被执行到的行数")
    total: int = Field(description="新增行里 JaCoCo 有探针记录的行数（分母）")
    pct: float | None = Field(default=None, description="百分比；分母为 0 时为 null")
    unmatched: int = Field(description="diff 里有、报告里找不到的源码文件数")
    ambiguous: int = Field(description="多条 diff 路径落到同一个报告文件的次数")


class UnitReportOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    version: str
    at: str
    instruction: float
    branch: float
    line: float
    covered: int
    total: int
    linesCovered: int
    linesTotal: int
    classesHit: int
    classesTotal: int
    xmlPath: str
    incCovered: int | None = None
    incTotal: int | None = None
    incPct: float | None = None


class UnitCoverageResult(BaseModel):
    ok: bool = True
    service: str
    report: UnitReportOut
    incremental: IncrementalBrief | None = None
    log: str


class DiffOut(BaseModel):
    id: int
    version: str
    base: str
    head: str | None = None
    at: str
    files: int
    addedLines: int


class DiffResult(BaseModel):
    ok: bool = True
    service: str
    diff: DiffOut
    matchesCurrentVersion: bool = Field(description="diff 的版本与服务当前 version 是否一致；不一致时运行时快照算不出新增覆盖")
    currentVersion: str | None = None
    runtime: IncrementalBrief | None = Field(default=None, description="重算后的运行时新增覆盖（该版本还没有快照时为 null）")
    unit: IncrementalBrief | None = None
    log: str
