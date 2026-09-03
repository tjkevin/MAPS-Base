-- MAPS MySQL 初始化加固脚本
-- 仅在 MySQL 数据目录首次初始化（空数据卷）时由官方镜像 entrypoint 自动执行一次；
-- 存量数据库不会重跑，需手动执行等价 SQL（见文末）。
--
-- 目的：消除 '%' 通配主机，把账号访问范围限制在 Docker 内网网段（172.16.0.0/12）。
-- 3306 端口已仅绑定 127.0.0.1，此为纵深防御；日常数据库管理请走 `docker exec maps-mysql mysql ...`（root@localhost）。
-- 注意：如 .env 自定义了 MYSQL_USER，请同步修改下面的 'maps'。

-- 应用账号：仅允许 Docker 容器网段连接
RENAME USER 'maps'@'%' TO 'maps'@'172.%';

-- root：禁止网段外远程登录（容器内 localhost 的 root@localhost 不受影响）
DROP USER IF EXISTS 'root'@'%';

FLUSH PRIVILEGES;

-- 存量库手动加固等价命令：
--   RENAME USER 'maps'@'%' TO 'maps'@'172.%';
--   DROP USER IF EXISTS 'root'@'%';
--   FLUSH PRIVILEGES;
