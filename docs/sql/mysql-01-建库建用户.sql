-- covhub 2.1 · MySQL 8 建库建用户
-- 由 DBA 用管理员账号执行一次。字符集必须是 utf8mb4（项目名允许中文，JSON 列也要它）。
--
-- 用法：
--   mysql -u root -p < mysql-01-建库建用户.sql
-- 执行前把下面的密码和允许连接的主机改掉。

CREATE DATABASE IF NOT EXISTS `covhub`
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_0900_ai_ci;

-- hub 进程用的账号。'%' 换成 hub 那台机器的地址更稳妥，如 'covhub'@'10.0.0.5'
CREATE USER IF NOT EXISTS 'covhub'@'%' IDENTIFIED BY '请改成强密码';

-- 日常运行只需要 DML；建表 / 升级表结构（covhub db upgrade）需要 DDL，
-- 两种方式二选一：
--   A. 给全权，让 hub 启动时自动建表 / 升级（database.autoUpgrade: true，默认）
GRANT ALL PRIVILEGES ON `covhub`.* TO 'covhub'@'%';
--   B. 只给 DML，表由 DBA 用 mysql-02-建表.sql 建好，hub 配置里 autoUpgrade: false
-- GRANT SELECT, INSERT, UPDATE, DELETE ON `covhub`.* TO 'covhub'@'%';

FLUSH PRIVILEGES;

-- hub 配置（covhub.yaml）里对应的连接串：
--   database:
--     url: "mysql+pymysql://covhub:<密码>@<mysql主机>:3306/covhub?charset=utf8mb4"
-- 或环境变量 COVHUB_DATABASE_URL（优先级高于配置文件）。
