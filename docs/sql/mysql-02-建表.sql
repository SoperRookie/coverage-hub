-- covhub 2.1 · MySQL 8 建表
-- 与 covhub/db/models.py 及 Alembic 迁移 0001 + 0002 完全等价（列、约束、索引、外键均一致）。
-- 最后写入 alembic_version = 0002，之后 hub 启动时的 `covhub db upgrade` 会认为结构已是最新，
-- 不会再重复建表；将来升级 covhub 时照常 `covhub db upgrade` 即可。
--
-- 用法（在 covhub 库里执行）：
--   mysql -u covhub -p covhub < mysql-02-建表.sql
--
-- 也可以不用这个文件：给账号 DDL 权限后 `python covhub.py db upgrade`（或直接 serve）会自动建出同样的表。
-- 两条路只能走一条 —— 表已存在时再跑 db upgrade 会因为「表已存在」失败。

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ---------------------------------------------------------------------------
-- 项目：服务分组，看板按它组织页面
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `projects` (
  `id`          INT          NOT NULL AUTO_INCREMENT,
  `name`        VARCHAR(100) NOT NULL COMMENT '项目名，URL 段与服务归属键，允许中文，建后不可改',
  `title`       VARCHAR(200) NULL     COMMENT '显示名',
  `description` TEXT         NULL,
  `created_at`  DATETIME     NOT NULL,
  `updated_at`  DATETIME     NOT NULL,
  CONSTRAINT `pk_projects` PRIMARY KEY (`id`),
  CONSTRAINT `uq_projects_name` UNIQUE (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='项目（服务分组）';

-- ---------------------------------------------------------------------------
-- 服务配置：原 targets.yaml 的 services[]
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `services` (
  `id`              INT          NOT NULL AUTO_INCREMENT,
  `name`            VARCHAR(100) NOT NULL COMMENT '服务名，主键语义；api/assets/index.html/favicon.ico 是保留名',
  `channel`         VARCHAR(8)   NOT NULL COMMENT 'pull | push',
  `version`         VARCHAR(100) NULL     COMMENT '当前在线版本，predeploy 归档目录名，pull 通道的 sessionid',
  `address`         VARCHAR(255) NULL     COMMENT 'pull：hub 连过去的地址',
  `port`            INT          NULL     COMMENT 'pull：agent 端口',
  `bind_address`    VARCHAR(255) NULL     COMMENT 'pull：agent 监听地址，NULL 按 0.0.0.0',
  `dump_retry`      INT          NULL     COMMENT 'jacococli dump --retry，NULL 按 3',
  `class_dump_dir`  VARCHAR(500) NULL     COMMENT '被测端 classdumpdir',
  `source_encoding` VARCHAR(32)  NULL     COMMENT '源码编码，NULL 按 UTF-8',
  `includes`        JSON         NOT NULL COMMENT '传给 agent 的插桩范围，字符串数组',
  `excludes`        JSON         NOT NULL,
  `classfiles`      JSON         NOT NULL COMMENT 'hub 上出报告用的 class 目录，字符串数组',
  `sourcefiles`     JSON         NOT NULL COMMENT 'hub 上的源码根目录，字符串数组',
  `report_excludes` JSON         NOT NULL COMMENT '报告端过滤，Ant 路径风格',
  `created_at`      DATETIME     NOT NULL,
  `updated_at`      DATETIME     NOT NULL,
  `project_id`      INT          NULL     COMMENT '所属项目，2.1 起；NULL = 未分组',
  CONSTRAINT `pk_services` PRIMARY KEY (`id`),
  CONSTRAINT `uq_services_name` UNIQUE (`name`),
  CONSTRAINT `fk_services_project_id_projects`
    FOREIGN KEY (`project_id`) REFERENCES `projects` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='服务配置';

-- ---------------------------------------------------------------------------
-- 服务运行态：与 services 1:1
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `service_state` (
  `service_id`    INT         NOT NULL,
  `session_start` VARCHAR(64) NULL     COMMENT '会话基线：exec 里 SessionInfo 的启动时刻，断代检测用',
  `push_mixed`    TINYINT(1)  NOT NULL COMMENT 'push 通道当前是否混版本',
  `updated_at`    DATETIME    NOT NULL,
  `online`        TINYINT(1)  NULL     COMMENT '采集线程每轮写入；NULL = 还没轮询过',
  `online_at`     DATETIME    NULL,
  CONSTRAINT `pk_service_state` PRIMARY KEY (`service_id`),
  CONSTRAINT `fk_service_state_service_id_services`
    FOREIGN KEY (`service_id`) REFERENCES `services` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='服务运行态';

-- ---------------------------------------------------------------------------
-- 快照：每次采集 / 结算 / 重出报告一行（原 state.json 的 history）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `snapshots` (
  `id`            BIGINT       NOT NULL AUTO_INCREMENT,
  `service_id`    INT          NOT NULL,
  `at`            DATETIME     NOT NULL,
  `kind`          VARCHAR(16)  NOT NULL COMMENT 'watch | dump | predeploy | seal | report',
  `version`       VARCHAR(100) NULL,
  `instruction`   DOUBLE       NOT NULL COMMENT '指令覆盖率 %',
  `branch`        DOUBLE       NOT NULL COMMENT '分支覆盖率 %',
  `covered`       BIGINT       NOT NULL COMMENT '已覆盖指令数',
  `total`         BIGINT       NOT NULL COMMENT '指令总数',
  `classes_hit`   INT          NOT NULL,
  `classes_total` INT          NOT NULL,
  `reason`        VARCHAR(32)  NULL     COMMENT 'seal 的原因，如 restart-detected',
  `session_start` VARCHAR(64)  NULL,
  `match_rate`    DOUBLE       NULL     COMMENT '结算时的指纹匹配率 %',
  `inc_covered`   INT          NULL     COMMENT '新增代码：已覆盖行',
  `inc_total`     INT          NULL     COMMENT '新增代码：可覆盖行；NULL = 没有该版本的 diff',
  `inc_pct`       DOUBLE       NULL     COMMENT '新增代码覆盖率 %；inc_total = 0 时为 NULL',
  CONSTRAINT `pk_snapshots` PRIMARY KEY (`id`),
  CONSTRAINT `fk_snapshots_service_id_services`
    FOREIGN KEY (`service_id`) REFERENCES `services` (`id`) ON DELETE CASCADE,
  INDEX `ix_snapshots_service_at` (`service_id`, `at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='覆盖率快照';

-- ---------------------------------------------------------------------------
-- 归档：每次结算一行（原 versions[] + manifest.json 的元数据）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `archives` (
  `id`             INT          NOT NULL AUTO_INCREMENT,
  `service_id`     INT          NOT NULL,
  `snapshot_id`    BIGINT       NOT NULL COMMENT '结算那一刻的快照',
  `version`        VARCHAR(100) NOT NULL,
  `archive_dir`    VARCHAR(255) NOT NULL COMMENT '相对服务目录，如 versions/1.4.2（同名退让成 -2）',
  `sealed_at`      DATETIME     NOT NULL,
  `sealed_by`      VARCHAR(32)  NOT NULL COMMENT 'predeploy | restart-detected',
  `fingerprint`    VARCHAR(16)  NULL     COMMENT 'class 产物指纹',
  `exec_count`     INT          NOT NULL,
  `match_rate`     DOUBLE       NULL,
  `health_verdict` TEXT         NULL,
  `merged`         VARCHAR(255) NULL     COMMENT 'merged.exec 文件名',
  CONSTRAINT `pk_archives` PRIMARY KEY (`id`),
  CONSTRAINT `uq_archives_service_dir` UNIQUE (`service_id`, `archive_dir`),
  CONSTRAINT `fk_archives_service_id_services`
    FOREIGN KEY (`service_id`) REFERENCES `services` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_archives_snapshot_id_snapshots`
    FOREIGN KEY (`snapshot_id`) REFERENCES `snapshots` (`id`) ON DELETE CASCADE,
  INDEX `ix_archives_service_sealed` (`service_id`, `sealed_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='结算归档';

-- ---------------------------------------------------------------------------
-- 断代记录：restart（pull，已自动封存）/ mixed-versions（push，只告警）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `breaks` (
  `id`           INT          NOT NULL AUTO_INCREMENT,
  `service_id`   INT          NOT NULL,
  `at`           DATETIME     NOT NULL,
  `kind`         VARCHAR(16)  NOT NULL COMMENT 'restart | mixed-versions',
  `from_session` VARCHAR(64)  NULL,
  `to_session`   VARCHAR(64)  NULL,
  `sealed_as`    VARCHAR(255) NULL     COMMENT '自动封存成的归档目录名',
  `archive_id`   INT          NULL,
  `instances`    INT          NULL     COMMENT 'mixed-versions 时的在线实例数',
  CONSTRAINT `pk_breaks` PRIMARY KEY (`id`),
  CONSTRAINT `fk_breaks_service_id_services`
    FOREIGN KEY (`service_id`) REFERENCES `services` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_breaks_archive_id_archives`
    FOREIGN KEY (`archive_id`) REFERENCES `archives` (`id`) ON DELETE SET NULL,
  INDEX `ix_breaks_service_at` (`service_id`, `at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='断代记录';

-- ---------------------------------------------------------------------------
-- 单测报告：构建流水线送来的 jacoco.xml 摘要（XML 本体在 data/<svc>/unit/<version>/）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `unit_reports` (
  `id`            INT          NOT NULL AUTO_INCREMENT,
  `service_id`    INT          NOT NULL,
  `version`       VARCHAR(100) NOT NULL,
  `at`            DATETIME     NOT NULL,
  `instruction`   DOUBLE       NOT NULL,
  `branch`        DOUBLE       NOT NULL,
  `line`          DOUBLE       NOT NULL,
  `covered`       BIGINT       NOT NULL,
  `total`         BIGINT       NOT NULL,
  `lines_covered` INT          NOT NULL,
  `lines_total`   INT          NOT NULL,
  `classes_hit`   INT          NOT NULL,
  `classes_total` INT          NOT NULL,
  `inc_covered`   INT          NULL,
  `inc_total`     INT          NULL,
  `inc_pct`       DOUBLE       NULL,
  `xml_path`      VARCHAR(500) NOT NULL COMMENT '相对服务目录，如 unit/1.4.3/jacoco.xml',
  CONSTRAINT `pk_unit_reports` PRIMARY KEY (`id`),
  CONSTRAINT `uq_unit_reports_service_version` UNIQUE (`service_id`, `version`),
  CONSTRAINT `fk_unit_reports_service_id_services`
    FOREIGN KEY (`service_id`) REFERENCES `services` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='单测报告摘要';

-- ---------------------------------------------------------------------------
-- git diff 摘要：行号明细只在磁盘 data/<svc>/diff/<version>.lines.json
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `diffs` (
  `id`          INT          NOT NULL AUTO_INCREMENT,
  `service_id`  INT          NOT NULL,
  `version`     VARCHAR(100) NOT NULL,
  `base`        VARCHAR(200) NOT NULL COMMENT '基线 ref / sha',
  `head`        VARCHAR(200) NULL,
  `at`          DATETIME     NOT NULL,
  `files`       INT          NOT NULL COMMENT '涉及的源码文件数',
  `added_lines` INT          NOT NULL COMMENT '新增行数',
  CONSTRAINT `pk_diffs` PRIMARY KEY (`id`),
  CONSTRAINT `uq_diffs_service_version` UNIQUE (`service_id`, `version`),
  CONSTRAINT `fk_diffs_service_id_services`
    FOREIGN KEY (`service_id`) REFERENCES `services` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='git diff 摘要';

-- ---------------------------------------------------------------------------
-- Alembic 版本标记：告诉 covhub 表结构已经是 0002（2.1.0 的最新迁移）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `alembic_version` (
  `version_num` VARCHAR(32) NOT NULL,
  CONSTRAINT `alembic_version_pkc` PRIMARY KEY (`version_num`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO `alembic_version` (`version_num`) VALUES ('0002')
  ON DUPLICATE KEY UPDATE `version_num` = '0002';

SET FOREIGN_KEY_CHECKS = 1;
