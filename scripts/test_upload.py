#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试上传功能脚本
"""

import requests
import os

def test_upload():
    """测试上传功能"""
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
    
    if login_response.status_code != 200:
        print("✗ 登录失败")
        return
    
    print("✓ 登录成功")
    
    # 2. 测试上传页面访问
    print("\n2. 测试上传页面...")
    upload_page_response = session.get(f"{base_url}/upload")
    print(f"上传页面状态码: {upload_page_response.status_code}")
    
    if upload_page_response.status_code == 200:
        print("✓ 上传页面访问成功")
    else:
        print("✗ 上传页面访问失败")
    
    # 3. 创建一个测试文件
    print("\n3. 创建测试文件...")
    test_file_path = "test_upload.txt"
    with open(test_file_path, 'w') as f:
        f.write("这是一个测试文件，用于测试上传功能。")
    
    # 4. 测试文件上传
    print("\n4. 测试文件上传...")
    try:
        with open(test_file_path, 'rb') as f:
            files = {'chunk': f}
            data = {
                'chunk_number': 0,
                'total_chunks': 1,
                'filename': 'test_upload.txt'
            }
            
            upload_response = session.post(f"{base_url}/api/upload/stream", files=files, data=data)
            print(f"上传API状态码: {upload_response.status_code}")
            print(f"上传响应: {upload_response.text}")
            
            if upload_response.status_code == 200:
                print("✓ 文件上传成功")
            else:
                print("✗ 文件上传失败")
    except Exception as e:
        print(f"✗ 上传测试失败: {e}")
    finally:
        # 清理测试文件
        if os.path.exists(test_file_path):
            os.remove(test_file_path)

if __name__ == '__main__':
    test_upload()


