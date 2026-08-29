#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SQLite数据导入脚本
将videos和subtitles文件夹中的数据导入到SQLite数据库中
"""

import os
import json
import re
from datetime import datetime
from app import app, db
from models import User, Recording, Task, TaskAssignment

def get_file_size(file_path):
    """获取文件大小（MB）"""
    try:
        size_bytes = os.path.getsize(file_path)
        return round(size_bytes / (1024 * 1024), 2)
    except:
        return 0

def get_duration_from_filename(filename):
    """从文件名中提取时长信息（如果有的话）"""
    # 这里可以根据实际需要实现时长提取逻辑
    # 目前返回一个默认值
    return 0.0

def read_srt_file(srt_path):
    """读取SRT字幕文件"""
    try:
        with open(srt_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        print(f"读取SRT文件失败 {srt_path}: {e}")
        return ""

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

def create_default_user():
    """创建默认用户"""
    try:
        # 检查是否已存在默认用户
        user = User.query.filter_by(username='admin').first()
        if not user:
            user = User(
                username='admin',
                email='admin@maps.com',
                role='admin'
            )
            user.set_password('admin123')
            db.session.add(user)
            db.session.commit()
            print("创建默认管理员用户: admin/admin123")
        return user
    except Exception as e:
        print(f"创建默认用户失败: {e}")
        return None

def create_sample_tasks():
    """创建示例任务"""
    try:
        # 检查是否已存在任务
        if Task.query.count() == 0:
            admin_user = User.query.filter_by(username='admin').first()
            if admin_user:
                # 创建示例任务
                task1 = Task(
                    title="视频转录任务",
                    description="对现有视频文件进行转录处理",
                    start_date=datetime.now(),
                    end_date=datetime(2024, 12, 31),
                    required_count=50,
                    task_type=1,
                    created_by=admin_user.id,
                    status='active'
                )
                
                task2 = Task(
                    title="字幕质量检查",
                    description="检查现有字幕文件的质量和准确性",
                    start_date=datetime.now(),
                    end_date=datetime(2024, 12, 31),
                    required_count=30,
                    task_type=2,
                    created_by=admin_user.id,
                    status='active'
                )
                
                db.session.add(task1)
                db.session.add(task2)
                db.session.commit()
                print("创建示例任务完成")
    except Exception as e:
        print(f"创建示例任务失败: {e}")

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
    
    # 导入数据
    admin_user = User.query.filter_by(username='admin').first()
    if not admin_user:
        print("未找到管理员用户，请先创建用户")
        return
    
    imported_count = 0
    for video_filename in video_files:
        try:
            video_path = os.path.join(videos_dir, video_filename)
            base_name = os.path.splitext(video_filename)[0]
            
            # 检查是否已存在该记录
            existing = Recording.query.filter_by(filename=video_filename).first()
            if existing:
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
            
            # 创建记录
            recording = Recording(
                filename=video_filename,
                file_path=video_path,
                duration=get_duration_from_filename(video_filename),
                recorded_by=admin_user.id,
                status='completed' if subtitle_content else 'pending',
                text_content=subtitle_content,
                subtitle_srt_path=subtitle_srt_path,
                subtitle_json_path=subtitle_json_path,
                subtitle_content=subtitle_content,
                subtitle_json_data=subtitle_json_data,
                created_at=datetime.now()
            )
            
            db.session.add(recording)
            imported_count += 1
            
            if imported_count % 10 == 0:
                print(f"已导入 {imported_count} 个文件...")
                
        except Exception as e:
            print(f"导入文件失败 {video_filename}: {e}")
            continue
    
    try:
        db.session.commit()
        print(f"成功导入 {imported_count} 个视频文件")
    except Exception as e:
        print(f"提交数据库失败: {e}")
        db.session.rollback()

def main():
    """主函数"""
    print("=== SQLite数据导入 ===")
    print("开始数据导入...")
    
    with app.app_context():
        # 创建数据库表
        print("创建数据库表...")
        db.create_all()
        print("数据库表创建完成")
        
        # 创建默认用户
        create_default_user()
        
        # 创建示例任务
        create_sample_tasks()
        
        # 导入视频和字幕数据
        import_videos_and_subtitles()
        
        # 显示统计信息
        total_recordings = Recording.query.count()
        completed_recordings = Recording.query.filter_by(status='completed').count()
        pending_recordings = Recording.query.filter_by(status='pending').count()
        
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
