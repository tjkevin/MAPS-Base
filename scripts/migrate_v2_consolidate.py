#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MAPS v2 表精简幂等迁移脚本（2026-08-31）

设计原则（非交互、幂等、可重复执行、最小破坏面）：
  1. db.create_all() 仅补建新表（user_credit_grants / compute_usage_logs / system_event_logs）；
  2. recordings 表按列检查后 ALTER ADD（缺哪列补哪列），并从 acquisition_metadata 回填；
  3. 旧数据迁移到新表（登录/账号审计/导出日志 -> system_event_logs；
     inspections -> audit_logs；audit_messages/task_messages -> inbox_messages），
     每条迁移均带 NOT EXISTS 防重；
  4. 迁移完成后旧表 RENAME TO *_legacy_20260831 备份，绝不 DROP；
  5. schema_migrations 版本标记表保证整段数据迁移只执行一次；
  6. 默认 dry-run 只打印计划，加 --apply 才真正执行。

用法（容器内）：
    python scripts/migrate_v2_consolidate.py            # 预检 / dry-run
    python scripts/migrate_v2_consolidate.py --apply    # 执行迁移
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app import app, db

LEGACY_SUFFIX = '_legacy_20260831'
VERSION = 'v2_consolidate_20260831'

# recordings 新增列（列名 -> 列 DDL）
NEW_RECORDING_COLS = {
    'uploader_id': "ADD COLUMN uploader_id INT NULL, ADD INDEX ix_recordings_uploader_id (uploader_id)",
    'file_type': "ADD COLUMN file_type VARCHAR(20) NULL, ADD INDEX ix_recordings_file_type (file_type)",
    'file_size': "ADD COLUMN file_size BIGINT NULL",
    'file_md5': "ADD COLUMN file_md5 VARCHAR(32) NULL, ADD INDEX ix_recordings_file_md5 (file_md5)",
    'source_channel': "ADD COLUMN source_channel VARCHAR(20) NULL DEFAULT 'upload'",
    'acquisition_task_no': "ADD COLUMN acquisition_task_no VARCHAR(64) NULL, ADD INDEX ix_recordings_acquisition_task_no (acquisition_task_no)",
    'acquisition_audit_status': "ADD COLUMN acquisition_audit_status VARCHAR(20) NULL DEFAULT 'pending', ADD INDEX ix_recordings_acquisition_audit_status (acquisition_audit_status)",
}

# 数据迁移 SQL（源表存在时才执行；均带防重保护）
DATA_MIGRATIONS = [
    (
        'login_logs -> system_event_logs',
        'login_logs',
        """
        INSERT INTO system_event_logs
            (event_type, actor_id, target_user_id, success, ip_address, action_type, summary, detail_json, created_at)
        SELECT 'login', NULLIF(l.user_id, 0), NULL, COALESCE(l.success, 1), l.ip_address,
               l.event_type,
               LEFT(COALESCE(l.message, l.username_attempted, l.event_type), 255),
               JSON_OBJECT('username_attempted', COALESCE(l.username_attempted, ''),
                           'user_agent', COALESCE(l.user_agent, '')),
               l.created_at
        FROM login_logs l
        WHERE NOT EXISTS (
            SELECT 1 FROM system_event_logs s
            WHERE s.event_type = 'login' AND s.action_type = l.event_type
              AND s.created_at <=> l.created_at
              AND s.ip_address <=> l.ip_address
              AND s.summary <=> LEFT(COALESCE(l.message, l.username_attempted, l.event_type), 255)
        )
        """,
    ),
    (
        'user_audit_logs -> system_event_logs',
        'user_audit_logs',
        """
        INSERT INTO system_event_logs
            (event_type, actor_id, target_user_id, success, action_type, summary, detail_json, created_at)
        SELECT 'user_audit', a.actor_id, a.target_user_id, 1, a.action_type,
               a.action_type, a.detail_json, a.created_at
        FROM user_audit_logs a
        WHERE NOT EXISTS (
            SELECT 1 FROM system_event_logs s
            WHERE s.event_type = 'user_audit' AND s.action_type = a.action_type
              AND s.created_at <=> a.created_at
              AND s.actor_id <=> a.actor_id
              AND s.target_user_id <=> a.target_user_id
        )
        """,
    ),
    (
        'export_logs -> system_event_logs',
        'export_logs',
        """
        INSERT INTO system_event_logs
            (event_type, actor_id, target_user_id, success, action_type, summary, detail_json, created_at)
        SELECT 'export', e.user_id, NULL, 1,
               CONCAT('export_', COALESCE(e.scope_type, 'unknown')),
               LEFT(CONCAT(COALESCE(e.export_format, ''), ':', COALESCE(e.scope_ref, ''),
                           ' rows=', COALESCE(e.row_count, 0)), 255),
               JSON_OBJECT('scope_type', COALESCE(e.scope_type, ''),
                           'scope_ref', COALESCE(e.scope_ref, ''),
                           'format', COALESCE(e.export_format, ''),
                           'fields_json', COALESCE(e.fields_json, ''),
                           'row_count', COALESCE(e.row_count, 0)),
               e.created_at
        FROM export_logs e
        WHERE NOT EXISTS (
            SELECT 1 FROM system_event_logs s
            WHERE s.event_type = 'export'
              AND s.created_at <=> e.created_at
              AND s.actor_id <=> e.user_id
              AND s.summary <=> LEFT(CONCAT(COALESCE(e.export_format, ''), ':', COALESCE(e.scope_ref, ''),
                                            ' rows=', COALESCE(e.row_count, 0)), 255)
        )
        """,
    ),
    (
        'inspections -> audit_logs',
        'inspections',
        """
        INSERT INTO audit_logs
            (recording_id, auditor_id, action_type, audit_result, comments, detail_json, created_at)
        SELECT i.recording_id, i.inspector_id, 'inspection',
               CASE WHEN i.status = 'approve' THEN 'approved'
                    WHEN i.status = 'reject' THEN 'rejected'
                    WHEN i.status IN ('approved', 'rejected', 'pending_fix') THEN i.status
                    ELSE 'approved' END,
               COALESCE(i.comments, ''),
               JSON_OBJECT('legacy_status', COALESCE(i.status, ''),
                           'corrections', COALESCE(i.corrections, '')),
               COALESCE(i.inspection_date, NOW())
        FROM inspections i
        WHERE NOT EXISTS (
            SELECT 1 FROM audit_logs a
            WHERE a.recording_id = i.recording_id AND a.action_type = 'inspection'
              AND a.auditor_id <=> i.inspector_id
              AND a.created_at <=> COALESCE(i.inspection_date, NOW())
        )
        """,
    ),
    (
        'audit_messages -> inbox_messages',
        'audit_messages',
        """
        INSERT INTO inbox_messages
            (recipient_id, sender_type, sender_id, category, msg_type, priority,
             title, summary, body, business_type, business_id, action_url,
             read_at, pinned_at, user_deleted_at, created_at)
        SELECT m.recipient_id, 'user', m.sender_id, 'audit', 'audit_reject_legacy', 'high',
               m.title, LEFT(COALESCE(m.body, ''), 200), COALESCE(m.body, ''),
               'recording', CAST(m.recording_id AS CHAR), '/manage',
               m.read_at, NULL, NULL, m.created_at
        FROM audit_messages m
        WHERE NOT EXISTS (
            SELECT 1 FROM inbox_messages b
            WHERE b.category = 'audit' AND b.recipient_id = m.recipient_id
              AND b.created_at <=> m.created_at AND b.title <=> m.title
        )
        """,
    ),
    (
        'task_messages -> inbox_messages',
        'task_messages',
        """
        INSERT INTO inbox_messages
            (recipient_id, sender_type, sender_id, category, msg_type, priority,
             title, summary, body, business_type, business_id, action_url,
             read_at, pinned_at, user_deleted_at, created_at)
        SELECT m.recipient_id, 'user', m.sender_id, 'task', 'task_legacy', 'medium',
               m.title, LEFT(COALESCE(m.body, ''), 200), COALESCE(m.body, ''),
               'task', CAST(m.task_id AS CHAR),
               CONCAT('/tasks?highlight=', m.task_id),
               m.read_at, NULL, NULL, m.created_at
        FROM task_messages m
        WHERE NOT EXISTS (
            SELECT 1 FROM inbox_messages b
            WHERE b.category = 'task' AND b.recipient_id = m.recipient_id
              AND b.created_at <=> m.created_at AND b.title <=> m.title
        )
        """,
    ),
]

LEGACY_TABLES = [
    'login_logs',
    'user_audit_logs',
    'acquisition_metadata',
    'inspections',
    'task_messages',
    'export_logs',
    'audit_messages',
    'message_channel_config',
]


def has_table(inspector, name):
    return name in inspector.get_table_names()


def has_column(inspector, table, column):
    return any(c['name'] == column for c in inspector.get_columns(table))


def marker_done(conn):
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version VARCHAR(64) PRIMARY KEY, applied_at DATETIME NOT NULL)"
    ))
    row = conn.execute(
        text("SELECT 1 FROM schema_migrations WHERE version = :v"),
        {'v': VERSION},
    ).first()
    return bool(row)


def main():
    apply = '--apply' in sys.argv
    with app.app_context():
        conn = db.engine.connect()
        inspector = db.inspect(db.engine)

        print('== MAPS v2 表精简迁移（%s）==' % ('APPLY' if apply else 'DRY-RUN'))
        print(f"数据库: {db.engine.url!s}")

        # 1) 新表 create_all（只增不改）
        if apply:
            db.create_all()
            conn = db.engine.connect()
            inspector = db.inspect(db.engine)
        print('[1] db.create_all() 补建新表（user_credit_grants/compute_usage_logs/system_event_logs）: %s'
              % ('已执行' if apply else '将执行'))

        # 2) recordings 缺列补齐
        if has_table(inspector, 'recordings'):
            missing = [c for c in NEW_RECORDING_COLS if not has_column(inspector, 'recordings', c)]
            if missing:
                print('[2] recordings 需补列: %s' % ', '.join(missing))
                if apply:
                    for col in missing:
                        ddl = 'ALTER TABLE recordings %s' % NEW_RECORDING_COLS[col]
                        print('    执行: %s' % ddl)
                        conn.execute(text(ddl))
                    conn.commit()
            else:
                print('[2] recordings 新列已齐全，跳过')
        else:
            print('[2] recordings 表不存在（全新库），跳过 ALTER')

        # 3) 从 acquisition_metadata 回填 recordings
        if has_table(inspector, 'acquisition_metadata') and has_table(inspector, 'recordings'):
            backfill_sql = text("""
                UPDATE recordings r
                JOIN acquisition_metadata m ON m.recording_id = r.id
                SET r.uploader_id = COALESCE(r.uploader_id, m.uploader_id),
                    r.file_type = COALESCE(r.file_type, m.file_type),
                    r.file_size = COALESCE(r.file_size, m.file_size),
                    r.file_md5 = COALESCE(r.file_md5, NULLIF(m.file_md5, '')),
                    r.source_channel = COALESCE(r.source_channel, m.source_channel),
                    r.acquisition_task_no = COALESCE(r.acquisition_task_no, m.task_no),
                    r.acquisition_audit_status = COALESCE(r.acquisition_audit_status, m.audit_status)
                WHERE r.acquisition_task_no IS NULL
            """)
            if apply:
                res = conn.execute(backfill_sql)
                conn.commit()
                print('[3] recordings 回填 acquisition_metadata 完成，影响行数: %s' % res.rowcount)
            else:
                print('[3] 将从 acquisition_metadata 回填 recordings（uploader/file_type/file_size/file_md5/source_channel/task_no/audit_status）')
        else:
            print('[3] acquisition_metadata 不存在，跳过回填')

        # 4) 数据迁移（版本标记控制一次性执行）
        already = marker_done(conn)
        conn.commit()
        if already:
            print('[4] 版本标记 %s 已存在，数据迁移与旧表改名均跳过（幂等）' % VERSION)
        else:
            for label, src_table, sql in DATA_MIGRATIONS:
                if not has_table(inspector, src_table):
                    print('[4] 跳过 %s（源表不存在）' % label)
                    continue
                if apply:
                    res = conn.execute(text(sql))
                    conn.commit()
                    print('[4] %s: 迁移 %s 行' % (label, res.rowcount))
                else:
                    print('[4] 将执行 %s' % label)

            # 5) 旧表 RENAME 备份（不 DROP）
            for t in LEGACY_TABLES:
                if not has_table(inspector, t):
                    print('[5] 跳过 %s（不存在，可能已改名）' % t)
                    continue
                new_name = t + LEGACY_SUFFIX
                if has_table(inspector, new_name):
                    print('[5] %s 已存在，跳过改名' % new_name)
                    continue
                if apply:
                    conn.execute(text('RENAME TABLE `%s` TO `%s`' % (t, new_name)))
                    conn.commit()
                    print('[5] %s -> %s' % (t, new_name))
                else:
                    print('[5] 将改名 %s -> %s（旧表保留备份，不 DROP）' % (t, new_name))

            if apply:
                conn.execute(
                    text("INSERT INTO schema_migrations (version, applied_at) VALUES (:v, :t)"),
                    {'v': VERSION, 't': datetime.utcnow()},
                )
                conn.commit()
                print('[6] 版本标记 %s 已写入' % VERSION)
            else:
                print('[6] 将写入版本标记 %s' % VERSION)

        conn.close()
        print('== 迁移%s完成 ==' % ('（APPLY）' if apply else '（DRY-RUN，未改动数据；确认无误后加 --apply 执行）'))


if __name__ == '__main__':
    main()
