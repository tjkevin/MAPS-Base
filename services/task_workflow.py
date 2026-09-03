# -*- coding: utf-8 -*-
"""任务管理：工作流状态、分配、申领、复核、评分、归档。"""
import json
from datetime import datetime, timedelta

from sqlalchemy import func, or_

from models import (
    db,
    User,
    Task,
    TaskAssignment,
    TaskWorkflowLog,
    TaskReturnRequest,
)
from services.messaging import notify_task_users

# 任务类别 -> 可执行/申领角色
CATEGORY_ROLE = {
    'collect': 'recorder',
    'process': 'recorder',
    'audit': 'inspector',
}

WORKFLOW_LABELS = {
    'draft': '草稿',
    'pending_assign': '待分配',
    'pending_claim': '待申领',
    'pending_execute': '待执行',
    'pending_review': '待复核',
    'pending_fix': '待修正',
    'pending_score': '待评分',
    'completed': '已完成',
    'archived': '已归档',
    'paused': '已暂停',
    'terminated': '已终止',
}


def effective_workflow_status(task):
    ws = task.workflow_status
    if ws:
        return ws
    if task.status == 'completed':
        return 'completed'
    if task.status == 'active':
        return 'pending_execute'
    return 'draft'


def sync_legacy_status(task):
    ws = effective_workflow_status(task)
    if ws in ('completed', 'archived'):
        task.status = 'completed'
    elif ws in ('terminated',):
        task.status = 'expired'
    elif ws == 'draft':
        task.status = task.status or 'pending'
    else:
        task.status = 'active'


def log_task_action(task, user_id, action_type, from_status, to_status, detail=None):
    row = TaskWorkflowLog(
        task_id=task.id,
        user_id=user_id,
        action_type=action_type,
        from_status=from_status,
        to_status=to_status,
        detail_json=json.dumps(detail, ensure_ascii=False) if detail else None,
    )
    db.session.add(row)


def total_completed_for_task(task_id):
    q = db.session.query(func.coalesce(func.sum(TaskAssignment.completed_count), 0)).filter(
        TaskAssignment.task_id == task_id
    )
    return int(q.scalar() or 0)


def total_quota_for_task(task_id):
    q = db.session.query(func.coalesce(func.sum(TaskAssignment.target_quota), 0)).filter(
        TaskAssignment.task_id == task_id
    )
    return int(q.scalar() or 0)


def validate_task_config(task):
    if task.end_date and task.start_date and task.end_date < task.start_date:
        return False, '截止时间不得早于开始时间'
    if not task.required_count or task.required_count < 1:
        return False, '目标完成数量须大于 0'
    if task.task_category not in CATEGORY_ROLE:
        return False, '任务类型无效'
    return True, None


def can_create_task(user):
    # 反馈#7 权限矩阵：任务创建与发布仅系统管理员/超级管理员（任务发起人），采集人员无发布权
    return user.role in ('admin', 'super_admin')


def is_task_creator_or_admin(user, task):
    return user.role in ('admin', 'super_admin') or task.created_by == user.id


def eligible_users_for_category(category):
    # f3：过滤停用/注销/已过期账号，避免向僵尸账号分配/派发任务
    role = CATEGORY_ROLE.get(category, 'recorder')
    now = datetime.utcnow()
    return (
        User.query.filter(
            User.role == role,
            User.is_active.is_(True),
            User.is_locked_account.is_(False),
            or_(User.account_valid_until.is_(None), User.account_valid_until > now),
        )
        .all()
    )


def auto_assign(task, creator_id):
    users = eligible_users_for_category(task.task_category)
    if not users:
        return False, '无匹配角色的用户可分配'
    counts = {}
    for u in users:
        c = (
            TaskAssignment.query.join(Task, TaskAssignment.task_id == Task.id)
            .filter(
                TaskAssignment.user_id == u.id,
                TaskAssignment.status.in_(('assigned', 'in_progress')),
                or_(
                    Task.workflow_status.is_(None),
                    ~Task.workflow_status.in_(('completed', 'archived', 'terminated', 'draft')),
                ),
            )
            .count()
        )
        counts[u.id] = c
    users_sorted = sorted(users, key=lambda u: counts.get(u.id, 0))
    n = min(len(users_sorted), max(1, min(5, task.required_count)))
    base = task.required_count // n
    rem = task.required_count - base * n
    for i, u in enumerate(users_sorted[:n]):
        # f3：唯一约束兜底防护，跳过已有分配记录的用户
        if TaskAssignment.query.filter_by(task_id=task.id, user_id=u.id).first():
            continue
        tq = base + (1 if i < rem else 0)
        if tq <= 0:
            continue
        db.session.add(
            TaskAssignment(
                task_id=task.id,
                user_id=u.id,
                target_quota=tq,
                completed_count=0,
                status='assigned',
            )
        )
    notify_task_users(
        [u.id for u in users_sorted[:n]],
        creator_id,
        task.id,
        f'任务已分配：{task.title}',
        f'您已被自动分配任务 {task.task_no or task.id}，目标 {task.required_count} 件，请查看「我的任务」。',
        msg_type='task_assigned_auto',
    )
    return True, None


def serialize_task_summary(task, include_assignments=False):
    ws = effective_workflow_status(task)
    done = total_completed_for_task(task.id)
    req = task.required_count or 0
    pct = min(100, int(done * 100 / req)) if req else 0
    now = datetime.utcnow()
    urgent = task.end_date and (task.end_date - now) <= timedelta(hours=24) and ws not in (
        'completed',
        'archived',
        'terminated',
    )
    row = {
        'id': task.id,
        'task_no': task.task_no,
        'title': task.title,
        'description': task.description,
        'acceptance_criteria': task.acceptance_criteria,
        'start_date': task.start_date.isoformat() if task.start_date else None,
        'end_date': task.end_date.isoformat() if task.end_date else None,
        'required_count': req,
        'completed_count': done,
        'progress_percent': pct,
        'priority': task.priority,
        'task_category': task.task_category,
        'task_subtype': task.task_subtype,
        'assign_mode': task.assign_mode,
        'workflow_status': ws,
        'workflow_label': WORKFLOW_LABELS.get(ws, ws),
        'status': task.status,
        'created_by': task.created_by,
        'published_at': task.published_at.isoformat() if task.published_at else None,
        'quality_score': task.quality_score,
        'urgent_deadline': urgent,
        'task_type': task.task_type,
    }
    if include_assignments:
        assigns = TaskAssignment.query.filter_by(task_id=task.id).all()
        row['assignments'] = [
            {
                'id': a.id,
                'user_id': a.user_id,
                'username': User.query.get(a.user_id).username if User.query.get(a.user_id) else '',
                'completed_count': a.completed_count,
                'target_quota': a.target_quota,
                'status': a.status,
                'claimed_from_pool': a.claimed_from_pool,
            }
            for a in assigns
        ]
    return row
