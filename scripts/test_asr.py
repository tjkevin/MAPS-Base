import os
import sys
from asr_processor import ASRProcessor

def test_asr():
    """测试ASR功能"""
    print("开始测试ASR语音识别功能...")
    
    # 检查videos目录
    if not os.path.exists("videos"):
        print("错误: videos目录不存在")
        return
    
    # 查找视频文件
    video_files = []
    for file in os.listdir("videos"):
        if file.lower().endswith(('.mp4', '.avi', '.mkv', '.flv', '.webm')):
            video_files.append(os.path.join("videos", file))
    
    if not video_files:
        print("错误: 在videos目录中未找到视频文件")
        return
    
    print(f"找到 {len(video_files)} 个视频文件")
    
    # 创建ASR处理器
    try:
        processor = ASRProcessor(model_size="base")
        print("ASR处理器初始化成功")
    except Exception as e:
        print(f"ASR处理器初始化失败: {str(e)}")
        return
    
    # 测试处理第一个视频文件
    test_video = video_files[0]
    print(f"\n测试处理视频: {os.path.basename(test_video)}")
    
    try:
        result = processor.process_video(test_video, "subtitles")
        if result:
            print(f"测试成功！字幕文件已生成: {result}")
        else:
            print("测试失败：未能生成字幕文件")
    except Exception as e:
        print(f"测试过程中出错: {str(e)}")

if __name__ == "__main__":
    test_asr()

