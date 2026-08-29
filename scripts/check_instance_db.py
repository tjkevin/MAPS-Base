#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检查instance目录中的数据库
"""

import sqlite3
import os

def check_instance_db():
    """检查instance目录中的数据库"""
    db_path = 'instance/maps.db'
    
    if not os.path.exists(db_path):
        print(f"数据库文件不存在: {db_path}")
        return
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    try:
        # 获取所有表名
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        
        print("数据库中的表:")
        for table in tables:
            print(f"- {table[0]}")
            
        print("\n" + "="*50)
        
        # 检查recordings表
        if ('recordings',) in tables:
            print("\nrecordings表的记录:")
            cursor.execute("SELECT id, filename, file_path, status, created_at FROM recordings ORDER BY id DESC LIMIT 10")
            records = cursor.fetchall()
            
            for record in records:
                id, filename, file_path, status, created_at = record
                print(f"ID: {id}")
                print(f"文件名: {filename}")
                print(f"文件路径: {file_path}")
                print(f"状态: {status}")
                print(f"创建时间: {created_at}")
                print("-" * 80)
        else:
            print("recordings表不存在")
            
    except Exception as e:
        print(f"检查数据库时出错: {e}")
    finally:
        conn.close()

if __name__ == '__main__':
    check_instance_db()


