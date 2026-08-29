#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Add the current directory to the Python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from models import db, Recording, User
from config import Config

def import_videos_from_directory():
    """Import all video files from the ./videos/ directory into the database"""
    
    # Create database engine - use the instance database
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', 'maps.db')
    db_uri = f"sqlite:///{db_path}"
    engine = create_engine(db_uri)
    
    # Create session
    Session = sessionmaker(bind=engine)
    session = Session()
    
    # Get admin user (assuming admin user exists)
    admin_user = session.query(User).filter_by(role='admin').first()
    if not admin_user:
        print("Error: No admin user found in database")
        return
    
    videos_dir = "./videos"
    
    if not os.path.exists(videos_dir):
        print(f"Error: Directory {videos_dir} does not exist")
        return
    
    # Get all video files
    video_extensions = ('.mp4', '.webm', '.avi', '.mov', '.flv', '.mkv', '.wmv')
    video_files = []
    
    for filename in os.listdir(videos_dir):
        if filename.lower().endswith(video_extensions):
            file_path = os.path.join(videos_dir, filename)
            if os.path.isfile(file_path):
                video_files.append((filename, file_path))
    
    print(f"Found {len(video_files)} video files in {videos_dir}")
    
    # Check which videos are already in the database
    existing_videos = session.query(Recording).filter(
        Recording.file_path.like(f"{os.path.abspath(videos_dir)}%")
    ).all()
    
    existing_filenames = {os.path.basename(r.file_path) for r in existing_videos}
    print(f"Found {len(existing_filenames)} videos already in database")
    
    # Import new videos
    imported_count = 0
    skipped_count = 0
    
    for filename, file_path in video_files:
        if filename in existing_filenames:
            print(f"Skipping {filename} (already exists)")
            skipped_count += 1
            continue
        
        try:
            # Get file size
            file_size = os.path.getsize(file_path)
            
            # Create new recording entry
            recording = Recording(
                filename=filename,
                file_path=os.path.abspath(file_path),
                duration=0,  # Will be calculated later if needed
                recorded_by=admin_user.id,
                recorded_at=datetime.now(),
                status='pending',
                text_content='',
                task_type='video_processing',
                created_at=datetime.now()
            )
            
            session.add(recording)
            imported_count += 1
            print(f"Imported: {filename}")
            
        except Exception as e:
            print(f"Error importing {filename}: {str(e)}")
    
    # Commit changes
    try:
        session.commit()
        print(f"\nImport completed:")
        print(f"- Imported: {imported_count} videos")
        print(f"- Skipped: {skipped_count} videos (already exist)")
        print(f"- Total videos in database: {session.query(Recording).count()}")
    except Exception as e:
        session.rollback()
        print(f"Error committing changes: {str(e)}")
    finally:
        session.close()

if __name__ == "__main__":
    print("Starting video import from ./videos/ directory...")
    import_videos_from_directory()
    print("Import process completed.")
