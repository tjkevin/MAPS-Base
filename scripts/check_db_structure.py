#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检查数据库结构
"""

import sqlite3

def check_db_structure():
    """检查数据库结构"""
    conn = sqlite3.connect('maps.db')
    cursor = conn.cursor()
    
    try:
        # 获取所有表名
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        
        print("数据库中的表:")
        for table in tables:
            print(f"- {table[0]}")
            
        print("\n" + "="*50)
        
        # 检查每个表的结构
        for table in tables:
            table_name = table[0]
            print(f"\n表 '{table_name}' 的结构:")
            cursor.execute(f"PRAGMA table_info({table_name});")
            columns = cursor.fetchall()
            for col in columns:
                print(f"  {col[1]} ({col[2]})")
                
            # 显示表中的记录数
            cursor.execute(f"SELECT COUNT(*) FROM {table_name};")
            count = cursor.fetchone()[0]
            print(f"  记录数: {count}")
            
    except Exception as e:
        print(f"检查数据库结构时出错: {e}")
    finally:
        conn.close()

if __name__ == '__main__':
    check_db_structure()


