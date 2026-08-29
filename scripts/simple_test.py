import requests
import json
from datetime import datetime

def test_simple_search():
    headers = {
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
    
    params = {
        "keyword": "采访",
        "page": 1,
        "order": "click",
        "duration": 4,
        "tids": 0,
        "search_type": "video"
    }
    
    try:
        response = requests.get("https://api.bilibili.com/x/web-interface/search/all", params=params, headers=headers, timeout=15)
        print(f"状态码: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"API响应代码: {data.get('code')}")
            
            # 提取视频列表
            result = []
            if isinstance(data, dict) and "data" in data:
                data_content = data["data"]
                if isinstance(data_content, dict) and "result" in data_content:
                    result_data = data_content["result"]
                    if isinstance(result_data, dict) and "video" in result_data:
                        result = result_data["video"]
            
            print(f"找到 {len(result)} 个视频")
            
            # 处理前5个视频
            for i, video in enumerate(result[:5]):
                try:
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
                    
                    video_info = {
                        "标题": video.get("title", "").replace("<em class=\"keyword\">", "").replace("</em>", ""),
                        "up主名称": video.get("author", ""),
                        "视频ID": video.get("aid", ""),
                        "时长(秒)": duration,
                        "视频链接": f"https://www.bilibili.com/video/{video.get('bvid', '')}",
                        "播放量": video.get("play", 0),
                    }
                    
                    if duration:
                        duration_str = f"{duration//60}分{duration%60}秒"
                    else:
                        duration_str = "未知"
                    
                    print(f"视频 {i+1}: {video_info['标题']} (时长: {duration_str})")
                except Exception as e:
                    print(f"处理视频 {i+1} 时出错: {str(e)}")
                    continue
        else:
            print(f"请求失败: {response.status_code}")
            
    except Exception as e:
        print(f"错误: {str(e)}")

if __name__ == "__main__":
    test_simple_search()
