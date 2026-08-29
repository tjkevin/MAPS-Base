import requests
import re
import json
import time
import random
import os
from urllib.parse import quote
from datetime import datetime

class BilibiliCrawler:
    def __init__(self):
        # 初始化爬虫设置
        self.base_url = "https://api.bilibili.com/x/web-interface/search/type"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Referer": "https://search.bilibili.com/",
            "Origin": "https://search.bilibili.com",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache"
        }
        # 存储抓取的数据
        self.data = []
        # 创建存储视频的目录
        if not os.path.exists("videos"):
            os.makedirs("videos")
    
    def search_videos(self, keywords, max_count=30, duration_limit=30):
        """
        根据关键词搜索视频
        :param keywords: 搜索关键词列表
        :param max_count: 最大视频数量
        :param duration_limit: 视频时长限制(分钟)
        :return: 搜索到的视频信息列表
        """
        all_videos = []
        
        for keyword in keywords:
            print(f"正在搜索关键词: {keyword}")
            
            # 尝试使用不同的API端点
            api_endpoints = [
                "https://api.bilibili.com/x/web-interface/search/type",
                "https://api.bilibili.com/x/web-interface/search/all/v2",
                "https://api.bilibili.com/x/web-interface/search/all"
            ]
            
            for api_url in api_endpoints:
                print(f"尝试API: {api_url}")
                page = 1
                retry_count = 0
                max_retries = 3
                
                while len(all_videos) < max_count and retry_count < max_retries:
                    # 构造API请求参数
                    params = {
                        "keyword": keyword,
                        "page": page,
                        "order": "click",
                        "duration": 2,  # 改为1-4分钟的视频
                        "tids": 0,
                        "search_type": "video"
                    }
                    
                    try:
                        # 添加随机延迟
                        time.sleep(random.uniform(2, 5))
                        
                        # 发送API请求
                        response = requests.get(api_url, params=params, headers=self.headers, timeout=15)
                        response.encoding = "utf-8"
                        
                        print(f"API响应状态码: {response.status_code}")
                        
                        if response.status_code == 412:
                            print(f"请求被拒绝(412)，尝试下一个API端点")
                            retry_count += 1
                            break
                        elif response.status_code != 200:
                            print(f"请求失败，状态码: {response.status_code}")
                            retry_count += 1
                            time.sleep(random.uniform(3, 6))
                            continue
                        
                        # 解析JSON响应
                        try:
                            data = response.json()
                            print(f"API响应: {data.get('code', 'unknown')}")
                        except json.JSONDecodeError:
                            print("JSON解析失败")
                            retry_count += 1
                            continue
                        
                        # 检查API响应状态
                        if data.get("code") != 0:
                            print(f"API返回错误: {data.get('message', '未知错误')}")
                            retry_count += 1
                            continue
                        
                        # 提取视频列表 - 处理正确的数据结构
                        result = []
                        if isinstance(data, dict) and "data" in data:
                            data_content = data["data"]
                            if isinstance(data_content, dict) and "result" in data_content:
                                result_data = data_content["result"]
                                if isinstance(result_data, dict) and "video" in result_data:
                                    result = result_data["video"]
                        
                        print(f"找到 {len(result)} 个视频结果")
                        
                        if not result:
                            print("没有更多视频了")
                            break
                        
                        for video in result:
                            try:
                                # 检查视频时长是否符合要求
                                duration = video.get("duration", 0)
                                
                                # 处理duration可能是字符串的情况
                                if isinstance(duration, str):
                                    try:
                                        # 尝试解析时间格式 "mm:ss" 或 "hh:mm:ss"
                                        time_parts = duration.split(':')
                                        if len(time_parts) == 2:  # mm:ss
                                            duration = int(time_parts[0]) * 60 + int(time_parts[1])
                                        elif len(time_parts) == 3:  # hh:mm:ss
                                            duration = int(time_parts[0]) * 3600 + int(time_parts[1]) * 60 + int(time_parts[2])
                                        else:
                                            duration = 0
                                    except:
                                        duration = 0
                                elif not isinstance(duration, (int, float)):
                                    duration = 0
                                
                                if duration and duration <= duration_limit * 60:  # 转换为秒
                                    video_info = {
                                        "标题": video.get("title", "").replace("<em class=\"keyword\">", "").replace("</em>", ""),
                                        "up主名称": video.get("author", ""),
                                        "视频ID": video.get("aid", ""),
                                        "原标签": video.get("tag", ""),
                                        "数据库标签": keyword,
                                        "文件类型": "视频",
                                        "时长(秒)": duration,
                                        "视频链接": f"https://www.bilibili.com/video/{video.get('bvid', '')}",
                                        "发布时间": datetime.fromtimestamp(video.get("pubdate", 0)).strftime('%Y-%m-%d %H:%M:%S'),
                                        "播放量": video.get("play", 0),
                                        "弹幕数": video.get("video_review", 0),
                                        "收藏数": video.get("favorites", 0)
                                    }
                                    all_videos.append(video_info)
                                    print(f"找到视频: {video_info['标题']} (时长: {duration//60}分{duration%60}秒)")
                                    
                                    if len(all_videos) >= max_count:
                                        break
                            except Exception as e:
                                print(f"处理视频数据时出错: {str(e)}")
                                print(f"视频数据: {video}")
                                continue
                        
                        page += 1
                        retry_count = 0  # 重置重试计数
                        
                    except Exception as e:
                        print(f"搜索出错: {str(e)}")
                        retry_count += 1
                        time.sleep(random.uniform(2, 4))
                
                # 如果成功获取到数据，跳出API端点循环
                if all_videos:
                    break
            
            # 达到最大数量则停止所有搜索
            if len(all_videos) >= max_count:
                break
        
        self.data = all_videos[:max_count]
        print(f"共找到 {len(self.data)} 个符合条件的视频")
        return self.data
    
    def get_video_download_url(self, bvid):
        """获取视频下载地址"""
        try:
            # 获取视频信息的API
            info_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
            response = requests.get(info_url, headers=self.headers)
            data = response.json()
            
            if data.get("code") != 0:
                print(f"获取视频信息失败: {data.get('message')}")
                return None
            
            # 获取视频下载地址
            cid = data["data"]["cid"]
            download_api = f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn=64"  # qn=64表示360P
            response = requests.get(download_api, headers=self.headers)
            download_data = response.json()
            
            if download_data.get("code") != 0:
                print(f"获取下载地址失败: {download_data.get('message')}")
                return None
            
            # 返回第一个视频片段的URL
            if "durl" in download_data["data"] and len(download_data["data"]["durl"]) > 0:
                return download_data["data"]["durl"][0]["url"]
            
            return None
        
        except Exception as e:
            print(f"获取下载地址出错: {str(e)}")
            return None
    
    def download_videos(self, max_download=30):
        """下载视频"""
        if not self.data:
            print("没有可下载的视频数据，请先搜索视频")
            return
        
        # 限制下载数量
        videos_to_download = self.data[:max_download]
        
        # 统计变量
        downloaded_count = 0
        skipped_count = 0
        failed_count = 0
        
        for i, video in enumerate(videos_to_download):
            try:
                print(f"正在处理第 {i+1}/{len(videos_to_download)} 个视频: {video['标题']}")
                
                # 构建文件名，移除特殊字符
                filename = re.sub(r'[\\/*?:"<>|]', "", video['标题']) + ".flv"
                filepath = os.path.join("videos", filename)
                
                # 检查文件是否已存在（支持多种格式）
                base_filename = re.sub(r'[\\/*?:"<>|]', "", video['标题'])
                possible_extensions = ['.flv', '.mp4', '.avi', '.mkv', '.webm']
                existing_file = None
                
                for ext in possible_extensions:
                    check_path = os.path.join("videos", base_filename + ext)
                    if os.path.exists(check_path):
                        existing_file = check_path
                        break
                
                if existing_file:
                    print(f"文件已存在，跳过下载: {os.path.basename(existing_file)}")
                    video["本地路径"] = existing_file
                    skipped_count += 1
                    continue
                
                # 从视频链接中提取bvid
                bvid_match = re.search(r'bvid=([^&]+)', video['视频链接'])
                if not bvid_match:
                    bvid_match = re.search(r'/video/([^/]+)', video['视频链接'])
                
                if not bvid_match:
                    print(f"无法提取视频ID: {video['视频链接']}")
                    continue
                
                bvid = bvid_match.group(1)
                
                # 获取下载地址
                download_url = self.get_video_download_url(bvid)
                if not download_url:
                    continue
                
                # 下载视频
                headers = self.headers.copy()
                headers["Referer"] = "https://www.bilibili.com/"  # 添加Referer头
                
                response = requests.get(download_url, headers=headers, stream=True)
                
                with open(filepath, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=1024*1024):  # 1MB chunks
                        if chunk:
                            f.write(chunk)
                
                print(f"视频下载完成: {filename}")
                video["本地路径"] = filepath
                downloaded_count += 1
                
                # 随机休眠一段时间，避免被屏蔽
                time.sleep(random.uniform(2, 5))
                
            except Exception as e:
                print(f"下载视频出错: {str(e)}")
                failed_count += 1
        
        # 打印下载统计
        print(f"\n下载统计:")
        print(f"成功下载: {downloaded_count} 个")
        print(f"跳过已存在: {skipped_count} 个")
        print(f"下载失败: {failed_count} 个")
        print(f"总计处理: {len(videos_to_download)} 个")
    
    def save_data(self, filename="bilibili_videos.json"):
        """保存抓取的数据到JSON文件"""
        if not self.data:
            print("没有数据可保存，请先搜索视频")
            return
        
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=4)
            print(f"数据已保存到 {filename}")
        except Exception as e:
            print(f"保存数据出错: {str(e)}")

if __name__ == "__main__":
    # 创建爬虫实例
    crawler = BilibiliCrawler()
    
    # 搜索关键词
    keywords = ["采访", "演讲", "讲话"]
    
    # 搜索视频，最多30个，时长30分钟以下
    crawler.search_videos(keywords, max_count=30, duration_limit=30)
    
    # 保存搜索到的视频信息
    crawler.save_data()
    
    # 下载视频，最多下载30个（可以根据需要调整）
    # 注意：大量下载可能会导致账号被限制，请谨慎使用
    crawler.download_videos(max_download=30)
