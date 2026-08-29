#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Add the current directory to the Python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from models import db, Recording

def link_subtitles_to_videos():
    """Link subtitle files with videos in the database"""
    
    # Create database engine - use the instance database
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', 'maps.db')
    db_uri = f"sqlite:///{db_path}"
    engine = create_engine(db_uri)
    
    # Create session
    Session = sessionmaker(bind=engine)
    session = Session()
    
    subtitles_dir = "./subtitles"
    
    if not os.path.exists(subtitles_dir):
        print(f"Error: Directory {subtitles_dir} does not exist")
        return
    
    # Get all subtitle files
    subtitle_files = []
    for filename in os.listdir(subtitles_dir):
        if filename.endswith(('.srt', '.json')):
            file_path = os.path.join(subtitles_dir, filename)
            if os.path.isfile(file_path):
                # Extract base name without extension
                base_name = os.path.splitext(filename)[0]
                subtitle_files.append((base_name, filename, file_path))
    
    print(f"Found {len(subtitle_files)} subtitle files in {subtitles_dir}")
    
    # Group by base name
    subtitle_groups = {}
    for base_name, filename, file_path in subtitle_files:
        if base_name not in subtitle_groups:
            subtitle_groups[base_name] = {}
        
        if filename.endswith('.srt'):
            subtitle_groups[base_name]['srt'] = (filename, file_path)
        elif filename.endswith('.json'):
            subtitle_groups[base_name]['json'] = (filename, file_path)
    
    print(f"Found {len(subtitle_groups)} unique video titles with subtitles")
    
    # Update database records
    updated_count = 0
    not_found_count = 0
    
    for base_name, subtitle_files in subtitle_groups.items():
        # Find corresponding video in database
        video = session.query(Recording).filter(
            Recording.filename.like(f"{base_name}%")
        ).first()
        
        if not video:
            print(f"Video not found for: {base_name}")
            not_found_count += 1
            continue
        
        try:
            # Update subtitle paths
            if 'srt' in subtitle_files:
                video.subtitle_srt_path = os.path.abspath(subtitle_files['srt'][1])
            
            if 'json' in subtitle_files:
                video.subtitle_json_path = os.path.abspath(subtitle_files['json'][1])
                
                # Read JSON subtitle content
                try:
                    with open(subtitle_files['json'][1], 'r', encoding='utf-8') as f:
                        json_content = f.read()
                        video.subtitle_json_data = json_content
                        
                        # Parse JSON to extract text content
                        try:
                            subtitle_data = json.loads(json_content)
                            if isinstance(subtitle_data, list):
                                # Extract text from subtitle segments
                                text_parts = []
                                for segment in subtitle_data:
                                    if isinstance(segment, dict) and 'text' in segment:
                                        text_parts.append(segment['text'])
                                video.subtitle_content = ' '.join(text_parts)
                            elif isinstance(subtitle_data, dict) and 'segments' in subtitle_data:
                                # Extract text from segments
                                text_parts = []
                                for segment in subtitle_data['segments']:
                                    if 'text' in segment:
                                        text_parts.append(segment['text'])
                                video.subtitle_content = ' '.join(text_parts)
                        except json.JSONDecodeError:
                            print(f"Error parsing JSON for {base_name}")
                except Exception as e:
                    print(f"Error reading JSON file for {base_name}: {str(e)}")
            
            updated_count += 1
            print(f"Updated: {video.filename}")
            
        except Exception as e:
            print(f"Error updating {base_name}: {str(e)}")
    
    # Commit changes
    try:
        session.commit()
        print(f"\nSubtitle linking completed:")
        print(f"- Updated: {updated_count} videos")
        print(f"- Not found: {not_found_count} videos")
    except Exception as e:
        session.rollback()
        print(f"Error committing changes: {str(e)}")
    finally:
        session.close()

if __name__ == "__main__":
    print("Starting subtitle linking...")
    link_subtitles_to_videos()
    print("Subtitle linking process completed.")


