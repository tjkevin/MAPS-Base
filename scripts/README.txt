脚本与测试 (scripts)
====================

本文件夹集中存放临时或辅助脚本，不参与主系统运行：

- 测试脚本：test_*.py、test_*.html
- 检查/修复：check_*.py、fix_database.py、final_solution.py
- 导入/工具：import_*.py、setup_database.py、simple_*.py
- 其他工具：calculate_video_duration.py、link_subtitles.py、subtitle_viewer.py、
  batch_asr.py、bilibili_crawler.py、generate_docx_from_draft.py

主程序入口与核心模块（app.py、config.py、models.py、asr_processor.py、init_database.py）
位于项目根目录。运行测试或工具时可在项目根目录执行，例如：
  python scripts/generate_docx_from_draft.py
  python scripts/check_db.py
