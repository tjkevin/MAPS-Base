#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import subprocess
import json
from pathlib import Path

def get_video_duration(file_path):
    """使用ffprobe获取视频时长"""
    try:
        # 使用ffprobe获取视频信息
        cmd = [
            'ffprobe',
            '-v', 'quiet',
            '-show_entries', 'format=duration',
            '-of', 'json',
            file_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        duration = float(data['format']['duration'])
        return duration
    except (subprocess.CalledProcessError, KeyError, ValueError) as e:
        print(f"无法获取 {file_path} 的时长: {e}")
        return 0

def format_duration(seconds):
    """将秒数转换为可读格式"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"

def calculate_total_duration():
    """计算videos目录下所有视频文件的总时长"""
    videos_dir = Path("./videos")
    
    if not videos_dir.exists():
        print("videos目录不存在")
        return
    
    # 支持的视频格式
    video_extensions = {'.flv', '.mp4', '.avi', '.mkv', '.mov', '.wmv', '.webm'}
    
    total_duration = 0
    video_count = 0
    file_details = []
    
    print("正在分析视频文件...")
    print("=" * 80)
    
    for file_path in videos_dir.iterdir():
        if file_path.is_file() and file_path.suffix.lower() in video_extensions:
            duration = get_video_duration(file_path)
            if duration > 0:
                total_duration += duration
                video_count += 1
                formatted_duration = format_duration(duration)
                file_size = file_path.stat().st_size
                file_size_mb = file_size / (1024 * 1024)
                
                file_details.append({
                    'name': file_path.name,
                    'duration': duration,
                    'formatted_duration': formatted_duration,
                    'size_mb': file_size_mb
                })
                
                print(f"{video_count:2d}. {file_path.name[:60]:<60} | {formatted_duration} | {file_size_mb:.1f}MB")
    
    print("=" * 80)
    print(f"\n统计结果:")
    print(f"视频文件总数: {video_count}")
    print(f"总时长: {format_duration(total_duration)} ({total_duration:.2f} 秒)")
    print(f"平均时长: {format_duration(total_duration/video_count) if video_count > 0 else '00:00:00'}")
    
    # 按时长排序显示前10个最长的视频
    if file_details:
        print(f"\n前10个最长的视频:")
        sorted_files = sorted(file_details, key=lambda x: x['duration'], reverse=True)
        for i, file_info in enumerate(sorted_files[:10], 1):
            print(f"{i:2d}. {file_info['name'][:50]:<50} | {file_info['formatted_duration']} | {file_info['size_mb']:.1f}MB")
    
    return total_duration, video_count

if __name__ == "__main__":
    try:
        calculate_total_duration()
    except KeyboardInterrupt:
        print("\n操作被用户中断")
    except Exception as e:
        print(f"发生错误: {e}")


