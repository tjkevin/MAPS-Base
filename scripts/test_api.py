#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试API脚本
"""

import requests
import json

def test_api():
    """测试API"""
    base_url = "http://127.0.0.1:5000"
    
    # 创建会话以保持cookies
    session = requests.Session()
    
    # 1. 测试登录
    print("1. 测试登录...")
    login_data = {
        'username': 'admin',
        'password': 'admin123'
    }
    
    login_response = session.post(f"{base_url}/login", data=login_data)
    print(f"登录状态码: {login_response.status_code}")
    
    if login_response.status_code == 200:
        print("✓ 登录成功")
    else:
        print("✗ 登录失败")
        return
    
    # 2. 测试数据API
    print("\n2. 测试数据API...")
    api_response = session.get(f"{base_url}/api/manage/data?filter=all&page=1")
    print(f"API状态码: {api_response.status_code}")
    
    if api_response.status_code == 200:
        try:
            data = api_response.json()
            print(f"✓ API调用成功")
            print(f"返回数据项数: {len(data.get('items', []))}")
            print(f"总记录数: {data.get('total', 0)}")
            
            if data.get('items'):
                print("第一条记录:")
                print(json.dumps(data['items'][0], indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"✗ 解析JSON失败: {e}")
    else:
        print(f"✗ API调用失败: {api_response.text}")
    
    # 3. 测试管理页面
    print("\n3. 测试管理页面...")
    manage_response = session.get(f"{base_url}/manage")
    print(f"管理页面状态码: {manage_response.status_code}")
    
    if manage_response.status_code == 200:
        print("✓ 管理页面访问成功")
    else:
        print("✗ 管理页面访问失败")

if __name__ == '__main__':
    test_api()
