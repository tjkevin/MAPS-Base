#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import json

def test_subtitles():
    """Test if videos with subtitles are properly displayed"""
    
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
    
    # Test data API - check multiple pages
    total_videos_with_subtitles = 0
    
    for page in range(1, 8):  # Check first 7 pages
        api_response = session.get(f'http://127.0.0.1:5000/api/manage/data?filter=all&page={page}')
        print(f"\nPage {page} status: {api_response.status_code}")
        
        if api_response.status_code == 200:
            data = api_response.json()
            print(f"Records on page {page}: {len(data['items'])}")
            
            # Check for videos with subtitles
            videos_with_subtitles = []
            for item in data['items']:
                if item['has_subtitle']:
                    videos_with_subtitles.append(item)
            
            print(f"Videos with subtitles on page {page}: {len(videos_with_subtitles)}")
            total_videos_with_subtitles += len(videos_with_subtitles)
            
            # Show first few videos with subtitles on this page
            for i, video in enumerate(videos_with_subtitles[:3]):
                print(f"  {i+1}. {video['filename']}")
                print(f"     ID: {video['id']}, Type: {video['type']}")
    
    print(f"\nTotal videos with subtitles found: {total_videos_with_subtitles}")

if __name__ == "__main__":
    test_subtitles()
