#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Add the current directory to the Python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from models import db, Recording

def check_subtitles():
    """Check subtitle information in the database"""
    
    # Create database engine - use the instance database
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', 'maps.db')
    db_uri = f"sqlite:///{db_path}"
    engine = create_engine(db_uri)
    
    # Create session
    Session = sessionmaker(bind=engine)
    session = Session()
    
    # Get all recordings
    recordings = session.query(Recording).all()
    
    print(f"Total recordings: {len(recordings)}")
    
    # Check videos with subtitles
    videos_with_subtitles = []
    videos_without_subtitles = []
    
    for recording in recordings:
        if recording.subtitle_content or recording.subtitle_srt_path or recording.subtitle_json_path:
            videos_with_subtitles.append(recording)
        else:
            videos_without_subtitles.append(recording)
    
    print(f"Videos with subtitles: {len(videos_with_subtitles)}")
    print(f"Videos without subtitles: {len(videos_without_subtitles)}")
    
    # Show first 10 videos with subtitles
    print("\nFirst 10 videos with subtitles:")
    for i, recording in enumerate(videos_with_subtitles[:10]):
        print(f"{i+1}. {recording.filename}")
        print(f"   ID: {recording.id}")
        print(f"   SRT path: {recording.subtitle_srt_path}")
        print(f"   JSON path: {recording.subtitle_json_path}")
        print(f"   Has content: {bool(recording.subtitle_content)}")
        print()
    
    # Show first 10 videos without subtitles
    print("\nFirst 10 videos without subtitles:")
    for i, recording in enumerate(videos_without_subtitles[:10]):
        print(f"{i+1}. {recording.filename}")
        print(f"   ID: {recording.id}")
        print(f"   File path: {recording.file_path}")
        print()
    
    session.close()

if __name__ == "__main__":
    check_subtitles()


