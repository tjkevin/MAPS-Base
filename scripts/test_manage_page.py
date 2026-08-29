#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import json

def test_manage_page():
    """Test the data management page functionality"""
    
    # Login
    session = requests.Session()
    login_data = {
        'username': 'admin',
        'password': 'admin123'
    }
    
    login_response = session.post('http://127.0.0.1:5000/login', data=login_data)
    print(f"Login status: {login_response.status_code}")
    
    if login_response.status_code != 200:
        print("Login failed")
        return
    
    # Test manage page
    manage_response = session.get('http://127.0.0.1:5000/manage')
    print(f"Manage page status: {manage_response.status_code}")
    
    if manage_response.status_code == 200:
        print("✓ Manage page loads successfully")
        
        # Check if the page contains the expected content
        content = manage_response.text
        
        # Check for key elements
        checks = [
            ("数据概览", "Title is correct"),
            ("dataList", "Data list container exists"),
            ("pagination", "Pagination container exists"),
            ("filterData", "Filter function exists"),
            ("loadData", "Load data function exists")
        ]
        
        for check_text, description in checks:
            if check_text in content:
                print(f"✓ {description}")
            else:
                print(f"✗ {description} - Missing: {check_text}")
        
        # Test API endpoint
        api_response = session.get('http://127.0.0.1:5000/api/manage/data?filter=all&page=1')
        if api_response.status_code == 200:
            data = api_response.json()
            print(f"✓ API returns {len(data.get('items', []))} items")
            print(f"✓ Total records: {data.get('total', 0)}")
            
            # Check for videos with subtitles
            videos_with_subtitles = [item for item in data.get('items', []) if item.get('has_subtitle')]
            print(f"✓ Videos with subtitles on this page: {len(videos_with_subtitles)}")
            
            # Show first few items
            print("\nFirst 3 items:")
            for i, item in enumerate(data.get('items', [])[:3]):
                print(f"  {i+1}. {item.get('filename', 'Unknown')} (ID: {item.get('id')})")
                print(f"     Type: {item.get('type')}, Status: {item.get('status')}")
                print(f"     Has subtitle: {item.get('has_subtitle')}")
        else:
            print(f"✗ API failed: {api_response.status_code}")
    else:
        print(f"✗ Manage page failed: {manage_response.status_code}")

if __name__ == "__main__":
    test_manage_page()


