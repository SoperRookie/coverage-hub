"""表结构。

跨库规则（MySQL 8 主验证，PostgreSQL / SQLite 也要能跑）：
- String 一律带长度，MySQL 建表硬要求；
- 百分比用 Double —— MySQL 的 FLOAT 是单精度，13.57 会变成 13.5700004；
- 列表用 JSON 列，Python 侧 default=list，**不设 server_default**（MySQL 8 的
  JSON 列不能带常量默认值），且任何查询都不按 JSON 列过滤或排序；
- 约束命名从第一版就固定下来：SQLite 改表只能走 batch 模式，没名字的约束
  Alembic 找不到。
"""

import os
from datetime import datetime

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, Double, ForeignKey, Index,
                        Integer, MetaData, String, Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def _now():
    return datetime.now().replace(microsecond=0)


# 服务配置在 dict（配置文件 / API 的 camelCase）与列（snake_case）之间的对应。
# 顺序就是 to_dict 输出的顺序，和 targets.example.yaml 一致，便于人眼比对。
SERVICE_SCALARS = (
    ("name", "name"), ("version", "version"), ("channel", "channel"),
    ("address", "address"), ("port", "port"), ("bindAddress", "bind_address"),
    ("classDumpDir", "class_dump_dir"), ("sourceEncoding", "source_encoding"),
    ("dumpRetry", "dump_retry"),
)
SERVICE_LISTS = (
    ("includes", "includes"), ("excludes", "excludes"),
    ("classfiles", "classfiles"), ("sourcefiles", "sourcefiles"),
    ("reportExcludes", "report_excludes"),
)
SERVICE_FIELDS = dict(SERVICE_SCALARS + SERVICE_LISTS)     # camelCase -> column


class Project(Base):
    """项目：若干服务的分组。删项目不删服务，服务的 project_id 置空。"""
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 default=_now, onupdate=_now)

    services: Mapped[list["Service"]] = relationship(back_populates="project")

    def to_dict(self):
        return {"id": self.id, "name": self.name, "title": self.title,
                "description": self.description,
                "createdAt": self.created_at.isoformat(timespec="seconds"),
                "updatedAt": self.updated_at.isoformat(timespec="seconds")}


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    channel: Mapped[str] = mapped_column(String(8), nullable=False, default="pull")
    version: Mapped[str | None] = mapped_column(String(100))
    address: Mapped[str | None] = mapped_column(String(255))
    port: Mapped[int | None] = mapped_column(Integer)
    bind_address: Mapped[str | None] = mapped_column(String(255))
    dump_retry: Mapped[int | None] = mapped_column(Integer)
    class_dump_dir: Mapped[str | None] = mapped_column(String(500))
    source_encoding: Mapped[str | None] = mapped_column(String(32))
    includes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    excludes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    classfiles: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    sourcefiles: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    report_excludes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 default=_now, onupdate=_now)

    state: Mapped["ServiceState"] = relationship(back_populates="service", uselist=False,
                                                 cascade="all, delete-orphan")
    project: Mapped[Project | None] = relationship(back_populates="services")

    def to_dict(self, base_dir=None):
        """还原成配置文件解析后的那种 svc dict。

        两条必须守住的语义：
        - 值为 None 的标量**不出现**在 dict 里。agent_opts 等处用的是
          svc.get("bindAddress", "0.0.0.0") 这种「键不存在才取默认」的写法；
        - classfiles / sourcefiles 库里存原文，这里相对配置文件目录展开 ——
          与旧 load_config 的规则一致，整个目录搬家时库里的相对路径照样成立。
        """
        d = {"id": self.id}
        for key, col in SERVICE_SCALARS:
            value = getattr(self, col)
            if value is not None:
                d[key] = value
        if self.project is not None:
            d["project"] = self.project.name
        for key, col in SERVICE_LISTS:
            d[key] = list(getattr(self, col) or [])
        if base_dir:
            for key in ("classfiles", "sourcefiles"):
                d[key] = [p if os.path.isabs(p) else os.path.normpath(os.path.join(base_dir, p))
                          for p in d[key]]
        return d

    def apply(self, fields):
        """按 camelCase 的字段名批量赋值（只动给到的键）。project 不是列，调用方先解析成 id。"""
        for key, value in fields.items():
            if key == "project":
                continue
            col = SERVICE_FIELDS[key]
            if key in dict(SERVICE_LISTS):
                value = list(value or [])
            setattr(self, col, value)


class ServiceState(Base):
    """运行态，与 services 1:1。单独一张表，配置的 UPDATE 不会误碰它。"""
    __tablename__ = "service_state"

    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), primary_key=True)
    # Java Date.toString() 原文，只比对不解析
    session_start: Mapped[str | None] = mapped_column(String(64))
    push_mixed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 采集线程每轮探活后写入；看板读这里，请求路径上不做 TCP 探活（离线服务一个 2 秒，页面会卡）
    online: Mapped[bool | None] = mapped_column(Boolean)
    online_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 default=_now, onupdate=_now)

    service: Mapped[Service] = relationship(back_populates="state")


class Snapshot(Base):
    """一次采集 / 结算 / 重出报告的统计结果（原 state.json 的 history）。"""
    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"),
                                    primary_key=True, autoincrement=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)   # dump|watch|predeploy|report|seal
    version: Mapped[str | None] = mapped_column(String(100))
    instruction: Mapped[float] = mapped_column(Double, nullable=False)
    branch: Mapped[float] = mapped_column(Double, nullable=False)
    covered: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total: Mapped[int] = mapped_column(BigInteger, nullable=False)
    classes_hit: Mapped[int] = mapped_column(Integer, nullable=False)
    classes_total: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(32))          # seal: restart-detected
    session_start: Mapped[str | None] = mapped_column(String(64))   # seal: 封存前的会话基线
    match_rate: Mapped[float | None] = mapped_column(Double)        # predeploy 体检结论
    # 本版本新增代码的覆盖：分母是 diff 新增行里 JaCoCo 有探针的行。没有 diff 时为 NULL
    inc_covered: Mapped[int | None] = mapped_column(Integer)
    inc_total: Mapped[int | None] = mapped_column(Integer)
    inc_pct: Mapped[float | None] = mapped_column(Double)

    __table_args__ = (Index("ix_snapshots_service_at", "service_id", "at"),)


class Archive(Base):
    """一次结算归档（原 state.json 的 versions[] + manifest.json 的元数据）。"""
    __tablename__ = "archives"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), nullable=False)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("snapshots.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    # 相对 <dataDir>/<service>/ 的目录，如 versions/1.4.2-2；同名归档退让成 -2 也靠它区分
    archive_dir: Mapped[str] = mapped_column(String(255), nullable=False)
    sealed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sealed_by: Mapped[str] = mapped_column(String(32), nullable=False)   # predeploy|restart-detected
    fingerprint: Mapped[str | None] = mapped_column(String(16))
    exec_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    match_rate: Mapped[float | None] = mapped_column(Double)
    health_verdict: Mapped[str | None] = mapped_column(Text)
    merged: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (
        UniqueConstraint("service_id", "archive_dir", name="uq_archives_service_dir"),
        Index("ix_archives_service_sealed", "service_id", "sealed_at"),
    )


class Break(Base):
    """断代记录。pull 是进程重启（已封存），push 是混版本（只告警）。"""
    __tablename__ = "breaks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)   # restart|mixed-versions
    from_session: Mapped[str | None] = mapped_column(String(64))
    to_session: Mapped[str | None] = mapped_column(String(64))
    sealed_as: Mapped[str | None] = mapped_column(String(255))
    archive_id: Mapped[int | None] = mapped_column(
        ForeignKey("archives.id", ondelete="SET NULL"))
    instances: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (Index("ix_breaks_service_at", "service_id", "at"),)


class UnitReport(Base):
    """构建流水线传上来的单元测试覆盖率（jacoco-aggregate 的 jacoco.xml）。XML 原文在磁盘上。"""
    __tablename__ = "unit_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    instruction: Mapped[float] = mapped_column(Double, nullable=False)
    branch: Mapped[float] = mapped_column(Double, nullable=False)
    line: Mapped[float] = mapped_column(Double, nullable=False)
    covered: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lines_covered: Mapped[int] = mapped_column(Integer, nullable=False)
    lines_total: Mapped[int] = mapped_column(Integer, nullable=False)
    classes_hit: Mapped[int] = mapped_column(Integer, nullable=False)
    classes_total: Mapped[int] = mapped_column(Integer, nullable=False)
    inc_covered: Mapped[int | None] = mapped_column(Integer)
    inc_total: Mapped[int | None] = mapped_column(Integer)
    inc_pct: Mapped[float | None] = mapped_column(Double)
    xml_path: Mapped[str] = mapped_column(String(500), nullable=False)   # 相对 <dataDir>/<svc>/

    __table_args__ = (UniqueConstraint("service_id", "version", name="uq_unit_reports_service_version"),)


class Diff(Base):
    """流水线传上来的 git diff 的摘要。行号明细落磁盘（大重构一次可达数 MB）。"""
    __tablename__ = "diffs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    base: Mapped[str] = mapped_column(String(200), nullable=False)
    head: Mapped[str | None] = mapped_column(String(200))
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    added_lines: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (UniqueConstraint("service_id", "version", name="uq_diffs_service_version"),)
