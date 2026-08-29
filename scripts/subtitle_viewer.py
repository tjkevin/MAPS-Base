import os
import json
import re
from datetime import datetime

class SubtitleViewer:
    def __init__(self, subtitles_dir="subtitles"):
        self.subtitles_dir = subtitles_dir
    
    def list_subtitles(self):
        """列出所有字幕文件"""
        if not os.path.exists(self.subtitles_dir):
            print("字幕目录不存在")
            return []
        
        subtitle_files = []
        for file in os.listdir(self.subtitles_dir):
            if file.endswith('.srt'):
                subtitle_files.append(file)
        
        return sorted(subtitle_files)
    
    def read_srt_subtitle(self, filename):
        """读取SRT字幕文件"""
        filepath = os.path.join(self.subtitles_dir, filename)
        if not os.path.exists(filepath):
            print(f"文件不存在: {filepath}")
            return []
        
        subtitles = []
        current_subtitle = {}
        
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            
            if line.isdigit():  # 字幕序号
                if current_subtitle:
                    subtitles.append(current_subtitle)
                current_subtitle = {'index': int(line)}
                i += 1
            elif '-->' in line:  # 时间戳
                time_parts = line.split(' --> ')
                current_subtitle['start_time'] = time_parts[0]
                current_subtitle['end_time'] = time_parts[1]
                i += 1
            elif line and i + 1 < len(lines) and not lines[i + 1].strip():  # 字幕内容
                current_subtitle['content'] = line
                i += 2
            else:
                i += 1
        
        if current_subtitle:
            subtitles.append(current_subtitle)
        
        return subtitles
    
    def read_json_subtitle(self, filename):
        """读取JSON字幕文件"""
        json_filename = filename.replace('.srt', '.json')
        filepath = os.path.join(self.subtitles_dir, json_filename)
        
        if not os.path.exists(filepath):
            return None
        
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def display_subtitle(self, filename, start_line=1, num_lines=20):
        """显示字幕内容"""
        subtitles = self.read_srt_subtitle(filename)
        if not subtitles:
            return
        
        print(f"\n字幕文件: {filename}")
        print(f"总条数: {len(subtitles)}")
        print("-" * 80)
        
        end_line = min(start_line + num_lines - 1, len(subtitles))
        
        for i in range(start_line - 1, end_line):
            subtitle = subtitles[i]
            print(f"{subtitle['index']:3d}. [{subtitle['start_time']} --> {subtitle['end_time']}]")
            print(f"     {subtitle['content']}")
            print()
    
    def search_subtitles(self, keyword, case_sensitive=False):
        """搜索字幕内容"""
        subtitle_files = self.list_subtitles()
        results = []
        
        for filename in subtitle_files:
            subtitles = self.read_srt_subtitle(filename)
            
            for subtitle in subtitles:
                content = subtitle['content']
                if not case_sensitive:
                    content = content.lower()
                    keyword = keyword.lower()
                
                if keyword in content:
                    result = {
                        'filename': filename,
                        'index': subtitle['index'],
                        'time': subtitle['start_time'],
                        'content': subtitle['content']
                    }
                    results.append(result)
        
        return results
    
    def display_search_results(self, keyword):
        """显示搜索结果"""
        results = self.search_subtitles(keyword)
        
        if not results:
            print(f"未找到包含 '{keyword}' 的字幕")
            return
        
        print(f"\n找到 {len(results)} 条包含 '{keyword}' 的字幕:")
        print("-" * 80)
        
        for i, result in enumerate(results, 1):
            print(f"{i:3d}. {result['filename']} - 第{result['index']}条 [{result['time']}]")
            print(f"     {result['content']}")
            print()
    
    def get_statistics(self):
        """获取字幕统计信息"""
        subtitle_files = self.list_subtitles()
        total_files = len(subtitle_files)
        total_subtitles = 0
        total_duration = 0
        
        for filename in subtitle_files:
            subtitles = self.read_srt_subtitle(filename)
            total_subtitles += len(subtitles)
            
            if subtitles:
                # 计算总时长（简单估算）
                last_subtitle = subtitles[-1]
                end_time = last_subtitle['end_time']
                # 解析时间格式 HH:MM:SS,mmm
                time_parts = end_time.replace(',', '.').split(':')
                hours = int(time_parts[0])
                minutes = int(time_parts[1])
                seconds = float(time_parts[2])
                duration = hours * 3600 + minutes * 60 + seconds
                total_duration += duration
        
        return {
            'total_files': total_files,
            'total_subtitles': total_subtitles,
            'total_duration_hours': total_duration / 3600,
            'avg_subtitles_per_file': total_subtitles / total_files if total_files > 0 else 0
        }

def main():
    viewer = SubtitleViewer()
    
    while True:
        print("\n" + "="*60)
        print("字幕查看器")
        print("="*60)
        print("1. 列出所有字幕文件")
        print("2. 查看字幕内容")
        print("3. 搜索字幕")
        print("4. 显示统计信息")
        print("5. 退出")
        print("-"*60)
        
        choice = input("请选择操作 (1-5): ").strip()
        
        if choice == '1':
            files = viewer.list_subtitles()
            if files:
                print(f"\n找到 {len(files)} 个字幕文件:")
                for i, filename in enumerate(files, 1):
                    print(f"{i:3d}. {filename}")
            else:
                print("未找到字幕文件")
        
        elif choice == '2':
            files = viewer.list_subtitles()
            if not files:
                print("未找到字幕文件")
                continue
            
            print("\n可用的字幕文件:")
            for i, filename in enumerate(files, 1):
                print(f"{i:3d}. {filename}")
            
            try:
                file_choice = int(input("\n请选择文件编号: ")) - 1
                if 0 <= file_choice < len(files):
                    filename = files[file_choice]
                    start_line = int(input("起始行号 (默认1): ") or "1")
                    num_lines = int(input("显示行数 (默认20): ") or "20")
                    viewer.display_subtitle(filename, start_line, num_lines)
                else:
                    print("无效的文件编号")
            except ValueError:
                print("请输入有效的数字")
        
        elif choice == '3':
            keyword = input("请输入搜索关键词: ").strip()
            if keyword:
                viewer.display_search_results(keyword)
            else:
                print("请输入搜索关键词")
        
        elif choice == '4':
            stats = viewer.get_statistics()
            print(f"\n字幕统计信息:")
            print(f"总文件数: {stats['total_files']}")
            print(f"总字幕条数: {stats['total_subtitles']}")
            print(f"总时长: {stats['total_duration_hours']:.2f} 小时")
            print(f"平均每文件字幕数: {stats['avg_subtitles_per_file']:.1f}")
        
        elif choice == '5':
            print("退出字幕查看器")
            break
        
        else:
            print("无效的选择，请重新输入")

if __name__ == "__main__":
    main()

