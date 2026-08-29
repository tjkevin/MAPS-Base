-- 方式 A：创建库 maps 与用户 maps（密码 maps_dev）
-- 使用：mysql -u root -p < scripts/mysql_create_maps_user.sql
-- 或在 MySQL Workbench / 客户端中整文件执行。

CREATE DATABASE IF NOT EXISTS maps CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'maps'@'localhost' IDENTIFIED BY 'maps_dev';
GRANT ALL PRIVILEGES ON maps.* TO 'maps'@'localhost';

-- Docker环境中用户从其他容器连接，需要授权 '%'
CREATE USER IF NOT EXISTS 'maps'@'%' IDENTIFIED BY 'maps_dev';
GRANT ALL PRIVILEGES ON maps.* TO 'maps'@'%';

FLUSH PRIVILEGES;
