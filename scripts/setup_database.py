#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键数据库设置脚本
自动完成数据库初始化和数据导入
"""

import sys
import os

def main():
    """主函数"""
    print("=== MAPS 数据库一键设置 ===")
    print()
    
    # 检查MySQL依赖
    try:
        import pymysql
        print("✓ PyMySQL 已安装")
    except ImportError:
        print("✗ PyMySQL 未安装，正在安装...")
        os.system("pip install PyMySQL cryptography")
        try:
            import pymysql
            print("✓ PyMySQL 安装成功")
        except ImportError:
            print("✗ PyMySQL 安装失败，请手动安装：pip install PyMySQL cryptography")
            return
    
    # 检查必要目录
    videos_dir = os.path.join(os.path.dirname(__file__), 'videos')
    subtitles_dir = os.path.join(os.path.dirname(__file__), 'subtitles')
    
    if not os.path.exists(videos_dir):
        print(f"✗ 视频目录不存在: {videos_dir}")
        return
    
    if not os.path.exists(subtitles_dir):
        print(f"✗ 字幕目录不存在: {subtitles_dir}")
        return
    
    print(f"✓ 视频目录: {videos_dir}")
    print(f"✓ 字幕目录: {subtitles_dir}")
    print()
    
    # 步骤1：初始化数据库
    print("步骤1: 初始化数据库...")
    try:
        from init_database import main as init_db
        init_db()
        print("✓ 数据库初始化完成")
    except Exception as e:
        print(f"✗ 数据库初始化失败: {e}")
        print("请检查MySQL服务是否正在运行，以及用户名密码是否正确")
        return
    
    print()
    
    # 步骤2：导入数据
    print("步骤2: 导入数据...")
    try:
        from import_data import main as import_data
        import_data()
        print("✓ 数据导入完成")
    except Exception as e:
        print(f"✗ 数据导入失败: {e}")
        return
    
    print()
    print("=== 设置完成 ===")
    print()
    print("默认登录信息:")
    print("用户名: admin")
    print("密码: admin123")
    print()
    print("现在可以启动应用:")
    print("python app.py")
    print()
    print("访问地址: http://localhost:5000")

if __name__ == '__main__':
    main()
