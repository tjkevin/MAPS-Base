#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
最终解决方案：正确创建数据库并导入数据
"""

import os
import json
from datetime import datetime
from app import app, db
from models import User, Recording, Task

def reset_database():
    """重置数据库"""
    with app.app_context():
        # 删除所有表
        db.drop_all()
        print("✓ 删除所有表")
        
        # 重新创建所有表
        db.create_all()
        print("✓ 重新创建所有表")

def create_default_user():
    """创建默认用户"""
    with app.app_context():
        user = User(
            username='admin',
            email='admin@maps.com',
            role='admin'
        )
        user.set_password('admin123')
        db.session.add(user)
        db.session.commit()
        print("✓ 创建默认管理员用户: admin/admin123")
        return user.id  # 返回用户ID而不是用户对象

def create_sample_tasks(admin_user_id):
    """创建示例任务"""
    with app.app_context():
        task1 = Task(
            title="视频转录任务",
            description="对现有视频文件进行转录处理",
            start_date=datetime.now(),
            end_date=datetime(2024, 12, 31),
            required_count=50,
            task_type=1,
            created_by=admin_user_id,
            status='active'
        )
        
        task2 = Task(
            title="字幕质量检查",
            description="检查现有字幕文件的质量和准确性",
            start_date=datetime.now(),
            end_date=datetime(2024, 12, 31),
            required_count=30,
            task_type=2,
            created_by=admin_user_id,
            status='active'
        )
        
        db.session.add(task1)
        db.session.add(task2)
        db.session.commit()
        print("✓ 创建示例任务完成")

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

def import_videos_and_subtitles(admin_user_id):
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
    
    print(f"✓ 找到 {len(video_files)} 个视频文件")
    
    # 获取所有字幕文件
    subtitle_files = {}
    for filename in os.listdir(subtitles_dir):
        if filename.lower().endswith('.json'):
            base_name = filename[:-5]  # 移除.json扩展名
            subtitle_files[base_name] = {
                'json': filename,
                'srt': base_name + '.srt' if os.path.exists(os.path.join(subtitles_dir, base_name + '.srt')) else None
            }
    
    print(f"✓ 找到 {len(subtitle_files)} 个字幕文件")
    
    with app.app_context():
        imported_count = 0
        for video_filename in video_files:
            try:
                video_path = os.path.join(videos_dir, video_filename)
                base_name = os.path.splitext(video_filename)[0]
                
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
                    duration=0.0,
                    recorded_by=admin_user_id,
                    status='completed' if subtitle_content else 'pending',
                    text_content=subtitle_content,
                    subtitle_srt_path=subtitle_srt_path,
                    subtitle_json_path=subtitle_json_path,
                    subtitle_content=subtitle_content,
                    subtitle_json_data=subtitle_json_data
                )
                
                db.session.add(recording)
                imported_count += 1
                
                if imported_count % 10 == 0:
                    print(f"✓ 已导入 {imported_count} 个文件...")
                    
            except Exception as e:
                print(f"导入文件失败 {video_filename}: {e}")
                continue
        
        try:
            db.session.commit()
            print(f"✓ 成功导入 {imported_count} 个视频文件")
        except Exception as e:
            print(f"提交数据库失败: {e}")
            db.session.rollback()

def main():
    """主函数"""
    print("=== 最终解决方案：重新创建数据库并导入数据 ===")
    
    # 重置数据库
    reset_database()
    
    # 创建默认用户
    admin_user_id = create_default_user()
    
    # 创建示例任务
    create_sample_tasks(admin_user_id)
    
    # 导入视频和字幕数据
    import_videos_and_subtitles(admin_user_id)
    
    # 显示统计信息
    with app.app_context():
        total_recordings = Recording.query.count()
        completed_recordings = Recording.query.filter_by(status='completed').count()
        pending_recordings = Recording.query.filter_by(status='pending').count()
        
        print("\n=== 导入完成 ===")
        print(f"总视频数: {total_recordings}")
        print(f"已完成: {completed_recordings}")
        print(f"待处理: {pending_recordings}")
        print("数据库创建和数据导入完成！")
        print()
        print("默认登录信息:")
        print("用户名: admin")
        print("密码: admin123")
        print()
        print("现在可以访问: http://localhost:5000")

if __name__ == '__main__':
    main()
