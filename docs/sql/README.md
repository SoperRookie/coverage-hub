# 建库建表 SQL（MySQL 8）

给公司内部部署时 DBA 用的脚本，两种方式二选一：

| 方式 | 做法 |
|---|---|
| **A · hub 自动建表**（默认） | 只执行 `mysql-01-建库建用户.sql`（给 `covhub` 账号该库的全部权限），hub 首次启动时 `covhub db upgrade` 自动建表；以后升级 covhub 也自动升表结构 |
| **B · DBA 手工建表** | 执行 `mysql-01-建库建用户.sql`（把 GRANT 换成只给 DML 的那行）+ `mysql-02-建表.sql`，hub 配置里 `database.autoUpgrade: false`；以后升级 covhub 时由 DBA 拿新版本的迁移 SQL 再执行 |

`mysql-02-建表.sql` 与 `covhub/db/models.py`、Alembic 迁移 `0001` + `0002` **完全等价**（列、类型、约束、索引、外键删除规则逐项比对过），
末尾写入 `alembic_version = 0002`，所以之后跑 `covhub db upgrade` / `db current` 会认为结构已是最新，不会重复建表。
**两条路只能走一条**：表已存在时再让 hub 自动建表会因为「表已存在」失败。

```bash
mysql -u root -p < mysql-01-建库建用户.sql          # 先改脚本里的密码和主机
mysql -u covhub -p covhub < mysql-02-建表.sql       # 方式 B 才需要
python covhub.py db current                          # hub 上核对：应显示 当前 0002（最新 0002）
```

PostgreSQL 的话不提供手写 SQL，直接让 hub 自动建表（方式 A）；SQLite 单机试用什么都不用做。

重新导出（改了 models / 迁移之后核对用）：

```bash
python - <<'PY'
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import mysql
from covhub.db.models import Base
for t in Base.metadata.sorted_tables:
    print(str(CreateTable(t).compile(dialect=mysql.dialect())).strip() + ";")
PY
```
