#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
简化的数据导入脚本
使用基本的SQLite操作导入数据
"""

import os
import json
import sqlite3
from datetime import datetime

def create_database():
    """创建数据库和表"""
    conn = sqlite3.connect('maps.db')
    cursor = conn.cursor()
    
    # 创建用户表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 创建录音表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS recordings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            duration REAL,
            recorded_by INTEGER,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT,
            text_content TEXT,
            task_type INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            subtitle_srt_path TEXT,
            subtitle_json_path TEXT,
            subtitle_content TEXT,
            subtitle_json_data TEXT
        )
    ''')
    
    # 创建任务表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            start_date TIMESTAMP NOT NULL,
            end_date TIMESTAMP NOT NULL,
            required_count INTEGER,
            task_type INTEGER,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT
        )
    ''')
    
    conn.commit()
    conn.close()
    print("数据库表创建完成")

def create_default_user():
    """创建默认用户"""
    conn = sqlite3.connect('maps.db')
    cursor = conn.cursor()
    
    # 检查是否已存在默认用户
    cursor.execute("SELECT id FROM users WHERE username = 'admin'")
    if not cursor.fetchone():
        cursor.execute('''
            INSERT INTO users (username, email, password_hash, role)
            VALUES (?, ?, ?, ?)
        ''', ('admin', 'admin@maps.com', 'pbkdf2:sha256:600000$...', 'admin'))
        print("创建默认管理员用户: admin/admin123")
    
    conn.commit()
    conn.close()

def read_json_file(json_path):
    """读取JSON字幕文件"""
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        print(f"读取JSON文件失败 {json_path}: {e}")
        return ""

def extract_subtitle_text(json_data):
    """从JSON数据中提取纯文本字幕"""
    try:
        data = json.loads(json_data)
        if isinstance(data, dict) and 'segments' in data:
            text_parts = []
            for segment in data['segments']:
                if 'text' in segment:
                    text_parts.append(segment['text'].strip())
            return ' '.join(text_parts)
        return ""
    except:
        return ""

def import_videos_and_subtitles():
    """导入视频和字幕数据"""
    videos_dir = os.path.join(os.path.dirname(__file__), 'videos')
    subtitles_dir = os.path.join(os.path.dirname(__file__), 'subtitles')
    
    if not os.path.exists(videos_dir):
        print(f"视频目录不存在: {videos_dir}")
        return
    
    if not os.path.exists(subtitles_dir):
        print(f"字幕目录不存在: {subtitles_dir}")
        return
    
    # 获取所有视频文件
    video_files = []
    for filename in os.listdir(videos_dir):
        if filename.lower().endswith(('.flv', '.mp4', '.avi', '.mov')):
            video_files.append(filename)
    
    print(f"找到 {len(video_files)} 个视频文件")
    
    # 获取所有字幕文件
    subtitle_files = {}
    for filename in os.listdir(subtitles_dir):
        if filename.lower().endswith('.json'):
            base_name = filename[:-5]  # 移除.json扩展名
            subtitle_files[base_name] = {
                'json': filename,
                'srt': base_name + '.srt' if os.path.exists(os.path.join(subtitles_dir, base_name + '.srt')) else None
            }
    
    print(f"找到 {len(subtitle_files)} 个字幕文件")
    
    # 连接数据库
    conn = sqlite3.connect('maps.db')
    cursor = conn.cursor()
    
    # 获取管理员用户ID
    cursor.execute("SELECT id FROM users WHERE username = 'admin'")
    admin_user = cursor.fetchone()
    if not admin_user:
        print("未找到管理员用户")
        conn.close()
        return
    
    admin_user_id = admin_user[0]
    
    imported_count = 0
    for video_filename in video_files:
        try:
            video_path = os.path.join(videos_dir, video_filename)
            base_name = os.path.splitext(video_filename)[0]
            
            # 检查是否已存在该记录
            cursor.execute("SELECT id FROM recordings WHERE filename = ?", (video_filename,))
            if cursor.fetchone():
                print(f"跳过已存在的文件: {video_filename}")
                continue
            
            # 查找对应的字幕文件
            subtitle_srt_path = None
            subtitle_json_path = None
            subtitle_content = ""
            subtitle_json_data = ""
            
            if base_name in subtitle_files:
                subtitle_info = subtitle_files[base_name]
                
                if subtitle_info['json']:
                    subtitle_json_path = os.path.join(subtitles_dir, subtitle_info['json'])
                    subtitle_json_data = read_json_file(subtitle_json_path)
                    subtitle_content = extract_subtitle_text(subtitle_json_data)
                
                if subtitle_info['srt']:
                    subtitle_srt_path = os.path.join(subtitles_dir, subtitle_info['srt'])
            
            # 插入记录
            cursor.execute('''
                INSERT INTO recordings (
                    filename, file_path, duration, recorded_by, status, 
                    text_content, subtitle_srt_path, subtitle_json_path, 
                    subtitle_content, subtitle_json_data, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                video_filename, video_path, 0.0, admin_user_id,
                'completed' if subtitle_content else 'pending',
                subtitle_content, subtitle_srt_path, subtitle_json_path,
                subtitle_content, subtitle_json_data, datetime.now()
            ))
            
            imported_count += 1
            
            if imported_count % 10 == 0:
                print(f"已导入 {imported_count} 个文件...")
                
        except Exception as e:
            print(f"导入文件失败 {video_filename}: {e}")
            continue
    
    conn.commit()
    conn.close()
    print(f"成功导入 {imported_count} 个视频文件")

def create_sample_tasks():
    """创建示例任务"""
    conn = sqlite3.connect('maps.db')
    cursor = conn.cursor()
    
    # 检查是否已存在任务
    cursor.execute("SELECT COUNT(*) FROM tasks")
    if cursor.fetchone()[0] == 0:
        # 获取管理员用户ID
        cursor.execute("SELECT id FROM users WHERE username = 'admin'")
        admin_user = cursor.fetchone()
        if admin_user:
            admin_user_id = admin_user[0]
            
            # 创建示例任务
            cursor.execute('''
                INSERT INTO tasks (title, description, start_date, end_date, 
                                 required_count, task_type, created_by, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                "视频转录任务", "对现有视频文件进行转录处理",
                datetime.now(), datetime(2024, 12, 31),
                50, 1, admin_user_id, 'active'
            ))
            
            cursor.execute('''
                INSERT INTO tasks (title, description, start_date, end_date, 
                                 required_count, task_type, created_by, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                "字幕质量检查", "检查现有字幕文件的质量和准确性",
                datetime.now(), datetime(2024, 12, 31),
                30, 2, admin_user_id, 'active'
            ))
            
            print("创建示例任务完成")
    
    conn.commit()
    conn.close()

def main():
    """主函数"""
    print("=== 简化数据导入 ===")
    print("开始数据导入...")
    
    # 创建数据库和表
    create_database()
    
    # 创建默认用户
    create_default_user()
    
    # 创建示例任务
    create_sample_tasks()
    
    # 导入视频和字幕数据
    import_videos_and_subtitles()
    
    # 显示统计信息
    conn = sqlite3.connect('maps.db')
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM recordings")
    total_recordings = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM recordings WHERE status = 'completed'")
    completed_recordings = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM recordings WHERE status = 'pending'")
    pending_recordings = cursor.fetchone()[0]
    
    conn.close()
    
    print("\n=== 导入完成 ===")
    print(f"总视频数: {total_recordings}")
    print(f"已完成: {completed_recordings}")
    print(f"待处理: {pending_recordings}")
    print("数据导入完成！")
    print()
    print("默认登录信息:")
    print("用户名: admin")
    print("密码: admin123")

if __name__ == '__main__':
    main()
