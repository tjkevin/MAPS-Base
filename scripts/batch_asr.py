import os
import sys
import time
import io

# Windows 控制台 GBK 下打印含特殊字符的文件名会报错，统一为 UTF-8 输出
if hasattr(sys.stdout, "buffer") and (not getattr(sys.stdout, "encoding", None) or sys.stdout.encoding.lower() != "utf-8"):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

# 保证从项目根目录可导入 asr_processor（无论当前工作目录是根还是 scripts）
_script_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_script_dir)
if _root not in sys.path:
    sys.path.insert(0, _root)
os.chdir(_root)  # 在项目根下执行，使 "videos" / "subtitles" 路径正确

from asr_processor import ASRProcessor


def batch_process_all_videos():
    """批量处理所有视频文件"""
    print("开始批量处理视频ASR语音识别...")
    
    # 检查videos目录
    if not os.path.exists("videos"):
        print("错误: videos目录不存在")
        return
    
    # 查找所有视频文件
    video_extensions = ['.mp4', '.avi', '.mkv', '.flv', '.webm']
    video_files = []
    
    for file in os.listdir("videos"):
        if any(file.lower().endswith(ext) for ext in video_extensions):
            video_files.append(os.path.join("videos", file))
    
    if not video_files:
        print("错误: 在videos目录中未找到视频文件")
        return
    
    print(f"找到 {len(video_files)} 个视频文件")
    
    # 检查subtitles目录
    subtitles_dir = "subtitles"
    if not os.path.exists(subtitles_dir):
        os.makedirs(subtitles_dir)
    
    # 检查已处理的文件
    processed_files = set()
    if os.path.exists(subtitles_dir):
        for file in os.listdir(subtitles_dir):
            if file.endswith('.srt'):
                # 移除.srt扩展名，获取对应的视频文件名
                # 支持多种视频格式
                video_name = file[:-4]  # 移除.srt扩展名
                processed_files.add(video_name)
    
    # 过滤出未处理的文件
    unprocessed_files = []
    for video_file in video_files:
        video_name = os.path.splitext(os.path.basename(video_file))[0]  # 获取不带扩展名的文件名
        if video_name not in processed_files:
            unprocessed_files.append(video_file)
        else:
            print(f"跳过已处理的文件: {os.path.basename(video_file)}")
    
    print(f"已处理: {len(processed_files)} 个")
    print(f"待处理: {len(unprocessed_files)} 个")
    
    if not unprocessed_files:
        print("所有视频都已处理完成！")
        return
    
    # 创建ASR处理器
    try:
        processor = ASRProcessor(model_size="base")
        print("ASR处理器初始化成功")
    except Exception as e:
        print(f"ASR处理器初始化失败: {str(e)}")
        return
    
    # 批量处理
    success_count = 0
    failed_count = 0
    
    for i, video_path in enumerate(unprocessed_files, 1):
        video_name = os.path.basename(video_path)
        print(f"\n{'='*60}")
        print(f"处理进度: {i}/{len(unprocessed_files)}")
        print(f"当前处理: {video_name}")
        print(f"{'='*60}")
        
        start_time = time.time()
        
        try:
            result = processor.process_video(video_path, subtitles_dir)
            if result:
                success_count += 1
                print(f"✅ 处理成功: {video_name}")
            else:
                failed_count += 1
                print(f"❌ 处理失败: {video_name}")
        except Exception as e:
            failed_count += 1
            print(f"❌ 处理出错: {video_name} - {str(e)}")
        
        end_time = time.time()
        processing_time = end_time - start_time
        print(f"处理耗时: {processing_time:.2f} 秒")
        
        # 显示进度统计
        print(f"\n进度统计:")
        print(f"成功: {success_count} 个")
        print(f"失败: {failed_count} 个")
        print(f"剩余: {len(unprocessed_files) - i} 个")
        
        # 添加延迟，避免过度占用资源
        if i < len(unprocessed_files):
            print("等待5秒后继续...")
            time.sleep(5)
    
    # 最终统计
    print(f"\n{'='*60}")
    print(f"批量处理完成！")
    print(f"总计: {len(unprocessed_files)} 个")
    print(f"成功: {success_count} 个")
    print(f"失败: {failed_count} 个")
    print(f"成功率: {success_count/len(unprocessed_files)*100:.1f}%")
    print(f"{'='*60}")

if __name__ == "__main__":
    batch_process_all_videos()
