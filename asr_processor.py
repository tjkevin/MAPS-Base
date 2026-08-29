import os
import json
import whisper
import torch
import numpy as np
from pydub import AudioSegment
from pydub.silence import split_on_silence
import librosa
from datetime import datetime
import re

class ASRProcessor:
    def __init__(self, model_size="base"):
        """
        初始化ASR处理器
        :param model_size: whisper模型大小 (tiny, base, small, medium, large)
        """
        self.model_size = model_size
        self.model = None
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"使用设备: {self.device}")
        
        # 加载whisper模型
        print(f"正在加载Whisper模型: {model_size}")
        self.model = whisper.load_model(model_size, device=self.device)
        print("模型加载完成")
    
    def extract_audio_from_video(self, video_path, audio_path=None):
        """
        从视频文件中提取音频
        :param video_path: 视频文件路径
        :param audio_path: 音频输出路径（可选）
        :return: 音频文件路径
        """
        if audio_path is None:
            audio_path = video_path.rsplit('.', 1)[0] + '.wav'
        
        print(f"正在从视频提取音频: {video_path}")
        
        try:
            # 使用pydub提取音频
            video = AudioSegment.from_file(video_path)
            audio = video.set_channels(1).set_frame_rate(16000)  # 转换为单声道，16kHz
            audio.export(audio_path, format="wav")
            print(f"音频提取完成: {audio_path}")
            return audio_path
        except Exception as e:
            print(f"音频提取失败: {str(e)}")
            return None
    
    def detect_speakers(self, audio_path, min_silence_len=800, silence_thresh=-35):
        """
        检测说话人变化（基于静音分割）
        :param audio_path: 音频文件路径
        :param min_silence_len: 最小静音长度（毫秒）
        :param silence_thresh: 静音阈值（dB）
        :return: 音频片段列表
        """
        print("正在检测说话人变化...")
        
        try:
            # 加载音频
            audio = AudioSegment.from_wav(audio_path)
            
            # 基于静音分割音频
            chunks = split_on_silence(
                audio,
                min_silence_len=min_silence_len,
                silence_thresh=silence_thresh,
                keep_silence=200  # 保留200ms的静音
            )
            
            # 过滤掉太短的片段
            filtered_chunks = [chunk for chunk in chunks if len(chunk) > 1000]  # 至少1秒
            
            print(f"检测到 {len(filtered_chunks)} 个有效音频片段")
            return filtered_chunks
        except Exception as e:
            print(f"说话人检测失败: {str(e)}")
            return None
    
    def transcribe_audio_chunk(self, audio_chunk, start_time_ms):
        """
        转写单个音频片段
        :param audio_chunk: 音频片段
        :param start_time_ms: 开始时间（毫秒）
        :return: 转写结果
        """
        try:
            # 将音频片段保存为临时文件
            temp_audio_path = f"temp_audio_{start_time_ms}.wav"
            audio_chunk.export(temp_audio_path, format="wav")
            
            # 使用whisper进行转写
            result = self.model.transcribe(
                temp_audio_path,
                language=None,  # 自动检测语言
                task="transcribe"
            )
            
            # 删除临时文件
            if os.path.exists(temp_audio_path):
                os.remove(temp_audio_path)
            
            # 计算时间戳
            start_time = start_time_ms / 1000.0
            end_time = (start_time_ms + len(audio_chunk)) / 1000.0
            
            return {
                "start_time": start_time,
                "end_time": end_time,
                "text": result["text"].strip(),
                "confidence": result.get("avg_logprob", 0),
                "language": result.get("language", "unknown")
            }
        except Exception as e:
            print(f"音频片段转写失败: {str(e)}")
            return None
    
    def assign_speakers(self, chunks):
        """
        智能分配说话人
        :param chunks: 音频片段列表
        :return: 说话人分配列表
        """
        speakers = []
        current_speaker = 1
        
        for i, chunk in enumerate(chunks):
            # 基于音频特征和时长来分配说话人
            # 这里使用简单的交替分配，实际应用中可以使用更复杂的算法
            if i > 0:
                # 如果当前片段与上一个片段时长差异很大，可能是不同说话人
                prev_duration = len(chunks[i-1])
                curr_duration = len(chunk)
                duration_ratio = max(prev_duration, curr_duration) / min(prev_duration, curr_duration)
                
                if duration_ratio > 2.0:  # 时长差异超过2倍
                    current_speaker = 3 - current_speaker  # 切换说话人
            
            speakers.append(f"说话人{current_speaker}")
        
        return speakers
    
    def process_video(self, video_path, output_dir="subtitles"):
        """
        处理视频文件，生成字幕
        :param video_path: 视频文件路径
        :param output_dir: 字幕输出目录
        :return: 字幕文件路径
        """
        if not os.path.exists(video_path):
            print(f"视频文件不存在: {video_path}")
            return None
        
        # 创建输出目录
        os.makedirs(output_dir, exist_ok=True)
        
        # 提取音频
        audio_path = self.extract_audio_from_video(video_path)
        if not audio_path:
            return None
        
        # 检测说话人变化
        audio_chunks = self.detect_speakers(audio_path)
        if not audio_chunks:
            print("未检测到有效的音频片段")
            return None
        
        # 分配说话人
        speakers = self.assign_speakers(audio_chunks)
        
        # 转写每个音频片段
        subtitles = []
        current_time = 0
        
        for i, (chunk, speaker) in enumerate(zip(audio_chunks, speakers)):
            print(f"正在转写片段 {i+1}/{len(audio_chunks)} ({speaker})")
            
            result = self.transcribe_audio_chunk(chunk, current_time)
            if result and result["text"]:
                subtitle_entry = {
                    "speaker": speaker,
                    "start_time": self.format_time(result["start_time"]),
                    "end_time": self.format_time(result["end_time"]),
                    "content": result["text"],
                    "confidence": result["confidence"],
                    "language": result["language"]
                }
                subtitles.append(subtitle_entry)
                print(f"  {speaker}: {result['text'][:50]}...")
            
            current_time += len(chunk)
        
        # 保存字幕文件
        video_name = os.path.splitext(os.path.basename(video_path))[0]
        subtitle_path = os.path.join(output_dir, f"{video_name}.srt")
        
        self.save_srt_subtitle(subtitle_path, subtitles)
        
        # 同时保存JSON格式
        json_path = os.path.join(output_dir, f"{video_name}.json")
        self.save_json_subtitle(json_path, subtitles)
        
        # 清理临时音频文件
        if os.path.exists(audio_path):
            os.remove(audio_path)
        
        print(f"字幕生成完成: {subtitle_path}")
        print(f"共生成 {len(subtitles)} 条字幕")
        return subtitle_path
    
    def format_time(self, seconds):
        """
        格式化时间为 HH:MM:SS,mmm 格式
        :param seconds: 秒数
        :return: 格式化的时间字符串
        """
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millisecs = int((seconds % 1) * 1000)
        
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millisecs:03d}"
    
    def save_srt_subtitle(self, filepath, subtitles):
        """
        保存SRT格式字幕文件
        :param filepath: 文件路径
        :param subtitles: 字幕列表
        """
        with open(filepath, 'w', encoding='utf-8') as f:
            for i, subtitle in enumerate(subtitles, 1):
                f.write(f"{i}\n")
                f.write(f"{subtitle['start_time']} --> {subtitle['end_time']}\n")
                f.write(f"[{subtitle['speaker']}] {subtitle['content']}\n")
                f.write("\n")
    
    def save_json_subtitle(self, filepath, subtitles):
        """
        保存JSON格式字幕文件
        :param filepath: 文件路径
        :param subtitles: 字幕列表
        """
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(subtitles, f, ensure_ascii=False, indent=2)
    
    def batch_process_videos(self, video_dir, output_dir="subtitles"):
        """
        批量处理视频文件
        :param video_dir: 视频目录
        :param output_dir: 输出目录
        """
        video_extensions = ['.mp4', '.avi', '.mkv', '.flv', '.webm']
        video_files = []
        
        # 查找所有视频文件
        for file in os.listdir(video_dir):
            if any(file.lower().endswith(ext) for ext in video_extensions):
                video_files.append(os.path.join(video_dir, file))
        
        print(f"找到 {len(video_files)} 个视频文件")
        
        # 检查已处理的文件
        processed_files = set()
        if os.path.exists(output_dir):
            for file in os.listdir(output_dir):
                if file.endswith('.srt'):
                    video_name = file[:-4]  # 移除.srt扩展名
                    processed_files.add(video_name)
        
        # 过滤出未处理的文件
        unprocessed_files = []
        for video_file in video_files:
            video_name = os.path.splitext(os.path.basename(video_file))[0]
            if video_name not in processed_files:
                unprocessed_files.append(video_file)
            else:
                print(f"跳过已处理的文件: {os.path.basename(video_file)}")
        
        print(f"已处理: {len(processed_files)} 个")
        print(f"待处理: {len(unprocessed_files)} 个")
        
        if not unprocessed_files:
            print("所有视频都已处理完成！")
            return
        
        # 处理每个未处理的视频文件
        for i, video_path in enumerate(unprocessed_files, 1):
            print(f"\n{'='*50}")
            print(f"处理视频 {i}/{len(unprocessed_files)}: {os.path.basename(video_path)}")
            print(f"{'='*50}")
            try:
                self.process_video(video_path, output_dir)
            except Exception as e:
                print(f"处理失败: {str(e)}")
                continue

if __name__ == "__main__":
    # 测试ASR处理器
    processor = ASRProcessor(model_size="base")
    
    # 处理单个视频文件
    # processor.process_video("videos/测试视频.mp4")
    
    # 批量处理视频文件
    processor.batch_process_videos("videos", "subtitles")
