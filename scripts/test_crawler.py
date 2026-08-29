import requests
import json

def test_bilibili_api():
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
            print(f"响应数据结构: {type(data)}")
            
            if isinstance(data, dict) and "data" in data:
                print(f"data字段类型: {type(data['data'])}")
                if isinstance(data["data"], dict):
                    print(f"data字段的键: {list(data['data'].keys())}")
                    
                    # 检查result字段
                    if "result" in data["data"]:
                        result = data["data"]["result"]
                        print(f"result字段类型: {type(result)}")
                        if isinstance(result, list):
                            print(f"找到 {len(result)} 个结果")
                            if result:
                                print(f"第一个结果: {result[0]}")
                        elif isinstance(result, dict):
                            print(f"result是字典，键: {list(result.keys())}")
                            if "video" in result:
                                videos = result["video"]
                                print(f"找到 {len(videos)} 个视频")
                                if videos:
                                    print(f"第一个视频: {videos[0]}")
                    else:
                        print("没有找到result字段")
                else:
                    print(f"data字段内容: {data['data']}")
            else:
                print("响应结构不符合预期")
                print(f"完整响应: {json.dumps(data, indent=2, ensure_ascii=False)[:1000]}")
        else:
            print(f"请求失败: {response.status_code}")
            
    except Exception as e:
        print(f"错误: {str(e)}")

if __name__ == "__main__":
    test_bilibili_api()
