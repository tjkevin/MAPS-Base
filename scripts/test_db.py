#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试数据库创建脚本
"""

from app import app, db
from models import User, Recording, Task

def test_database_creation():
    """测试数据库创建"""
    print("开始测试数据库创建...")
    
    with app.app_context():
        try:
            # 创建所有表
            db.create_all()
            print("✓ 数据库表创建成功")
            
            # 检查表是否存在
            import sqlite3
            conn = sqlite3.connect('maps.db')
            cursor = conn.cursor()
            
            # 获取所有表名
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = cursor.fetchall()
            print(f"✓ 创建的表: {[table[0] for table in tables]}")
            
            # 检查recordings表结构
            cursor.execute("PRAGMA table_info(recordings);")
            columns = cursor.fetchall()
            print("✓ recordings表结构:")
            for col in columns:
                print(f"  - {col[1]} ({col[2]})")
            
            conn.close()
            
            # 创建测试用户
            user = User(
                username='test_admin',
                email='test@maps.com',
                role='admin'
            )
            user.set_password('test123')
            db.session.add(user)
            db.session.commit()
            print("✓ 测试用户创建成功")
            
            # 创建测试记录
            recording = Recording(
                filename='test.flv',
                file_path='test/path.flv',
                duration=0.0,
                recorded_by=user.id,
                status='pending',
                text_content='',
                task_type=1
            )
            db.session.add(recording)
            db.session.commit()
            print("✓ 测试记录创建成功")
            
            # 查询测试
            count = Recording.query.count()
            print(f"✓ 查询测试成功，记录数: {count}")
            
        except Exception as e:
            print(f"✗ 错误: {e}")
            import traceback
            traceback.print_exc()

if __name__ == '__main__':
    test_database_creation()
