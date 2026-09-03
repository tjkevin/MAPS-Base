#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库初始化脚本
创建MySQL数据库和表结构
"""

import pymysql
from app import app, db
from models import User, Recording, Task, TaskAssignment

def create_database():
    """创建MySQL数据库"""
    try:
        # 连接到MySQL服务器（不指定数据库）
        connection = pymysql.connect(
            host='localhost',
            user='root',
            password='password',
            charset='utf8mb4'
        )
        
        cursor = connection.cursor()
        
        # 创建数据库
        cursor.execute("CREATE DATABASE IF NOT EXISTS maps CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        print("数据库 'maps' 创建成功")
        
        cursor.close()
        connection.close()
        
    except Exception as e:
        print(f"创建数据库失败: {e}")
        print("请确保MySQL服务正在运行，并且用户名和密码正确")
        return False
    
    return True

def init_tables():
    """初始化数据库表"""
    try:
        with app.app_context():
            # 创建所有表
            db.create_all()
            print("数据库表创建成功")
            return True
    except Exception as e:
        print(f"创建表失败: {e}")
        return False

def main():
    """主函数"""
    print("开始初始化数据库...")
    
    # 创建数据库
    if not create_database():
        return
    
    # 初始化表结构
    if not init_tables():
        return
    
    print("数据库初始化完成！")
    print("现在可以运行 python import_data.py 来导入数据")

if __name__ == '__main__':
    main()
