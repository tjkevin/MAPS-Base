#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检查数据库中的记录
"""

import sqlite3
from datetime import datetime

def check_recordings():
    """检查数据库中的录音记录"""
    conn = sqlite3.connect('maps.db')
    cursor = conn.cursor()
    
    try:
        cursor.execute('SELECT id, filename, file_path, status, created_at FROM recordings ORDER BY id DESC LIMIT 10')
        records = cursor.fetchall()
        
        print("最近的录音记录:")
        print("-" * 80)
        for record in records:
            id, filename, file_path, status, created_at = record
            print(f"ID: {id}")
            print(f"文件名: {filename}")
            print(f"文件路径: {file_path}")
            print(f"状态: {status}")
            print(f"创建时间: {created_at}")
            print("-" * 80)
            
    except Exception as e:
        print(f"查询数据库时出错: {e}")
    finally:
        conn.close()

if __name__ == '__main__':
    check_recordings()


