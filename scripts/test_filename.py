#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试文件名处理
"""

def clean_filename(filename):
    """清理文件名但保留原始名称"""
    # 移除路径分隔符和危险字符
    dangerous_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|']
    for char in dangerous_chars:
        filename = filename.replace(char, '_')
    return filename

def test_filename_processing():
    """测试文件名处理"""
    test_filenames = [
        "my_song.mp3",
        "song with spaces.mp3",
        "song-with-dashes.mp3",
        "song_with_underscores.mp3",
        "song(1).mp3",
        "song[1].mp3",
        "song{1}.mp3",
        "song<1>.mp3",
        "song>1.mp3",
        "song:1.mp3",
        "song*1.mp3",
        "song?1.mp3",
        "song\"1\".mp3",
        "song|1.mp3",
        "song/1.mp3",
        "song\\1.mp3",
        "中文歌曲.mp3",
        "歌曲 (1).mp3",
        "歌曲[1].mp3",
        "歌曲{1}.mp3",
        "歌曲<1>.mp3",
        "歌曲>1.mp3",
        "歌曲:1.mp3",
        "歌曲*1.mp3",
        "歌曲?1.mp3",
        "歌曲\"1\".mp3",
        "歌曲|1.mp3",
        "歌曲/1.mp3",
        "歌曲\\1.mp3",
    ]
    
    print("文件名处理测试:")
    print("=" * 60)
    
    for original in test_filenames:
        cleaned = clean_filename(original)
        print(f"原始: {original}")
        print(f"处理后: {cleaned}")
        print("-" * 40)

if __name__ == '__main__':
    test_filename_processing()


