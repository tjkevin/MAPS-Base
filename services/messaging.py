# -*- coding: utf-8 -*-
"""统一消息：站内信、模板渲染、投递审计、可选邮件/短信桩、任务截止扫描。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from sqlalchemy import and_, or_

from models import (
    AuditMessage,
    db,
    InboxMessage,
    MessageChannelConfig,
    MessageDeliveryLog,
    MessageTemplate,
    SystemAnnouncement,
    Task,
    TaskAssignment,
    TaskMessage,
    User,
)

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def render_vars(template: str | None, variables: dict) -> str:
    if not template:
        return ""

    def _sub(m):
        key = m.group(1)
        return str(variables.get(key, m.group(0)))

    return _PLACEHOLDER.sub(_sub, template)


def get_or_create_channel_config() -> MessageChannelConfig:
    c = db.session.get(MessageChannelConfig, 1)
    if not c:
        c = MessageChannelConfig(
            id=1,
            email_enabled=False,
            sms_enabled=False,
            email_roles_json=json.dumps([], ensure_ascii=False),
        )
        db.session.add(c)
        db.session.flush()
    return c


def load_template(template_key: str) -> MessageTemplate | None:
    return MessageTemplate.query.filter_by(template_key=template_key, enabled=True).first()


DEFAULT_TEMPLATE_SEEDS = [
    ("task_published_pool", "task", "【公海】新任务：{{title}}", "任务编号 {{task_no}}，截止 {{end_date}}，请申领。", "medium", "任务发布（公海）"),
    ("task_assigned", "task", "任务已分配：{{title}}", "您被指派任务 {{task_no}}，请执行。", "medium", "手动分配"),
    ("task_assigned_auto", "task", "任务已分配：{{title}}", "您已被自动分配任务 {{task_no}}，目标 {{required_count}} 件。", "medium", "自动分配"),
    ("task_claim_success", "task", "申领成功：{{title}}", "您已成功申领任务 {{task_no}}，配额 {{quota}}。", "medium", "公海申领"),
    ("task_submit_review", "task", "待复核：{{title}}", "任务 {{task_no}} 已提交复核。", "high", "提交复核"),
    ("task_review_pass", "task", "复核通过：{{title}}", "{{detail}}", "medium", "复核通过"),
    ("task_review_reject", "task", "复核打回：{{title}}", "审核意见：\n{{comments}}", "urgent", "复核打回"),
    ("task_scored", "task", "任务质量评分完成：{{title}}", "任务 {{task_no}} 评分：{{score}}。", "medium", "评分完成"),
    ("task_pending_archive", "task", "任务待归档：{{title}}", "任务 {{task_no}} 已完成评分，可归档。", "low", "待归档"),
    ("task_archived", "task", "任务已归档：{{title}}", "任务 {{task_no}} 已最终归档。", "low", "归档"),
    ("task_paused", "task", "任务已暂停：{{title}}", "{{reason}}", "high", "暂停"),
    ("task_terminated", "task", "任务已终止：{{title}}", "{{reason}}", "high", "终止"),
    ("task_return_request", "task", "退领申请：{{title}}", "{{reason}}", "medium", "退领申请"),
    ("task_return_approved", "task", "退领已通过：{{title}}", "{{detail}}", "medium", "退领通过"),
    ("task_return_rejected", "task", "退领未通过：{{title}}", "{{detail}}", "medium", "退领驳回"),
    ("task_deadline_24h", "task", "【预警】任务即将截止：{{title}}", "距离截止时间不足 24 小时（{{end_date}}）。", "high", "截止24h"),
    ("task_deadline_12h", "task", "【加急】任务即将截止：{{title}}", "距离截止时间不足 12 小时（{{end_date}}）。", "urgent", "截止12h"),
    ("task_quota_completed", "task", "个人配额已完成：{{title}}", "您在任务 {{task_no}} 下已达到个人目标完成量。", "medium", "个人配额完成"),
    ("audit_reject", "audit", "审核打回：请修正数据", "数据 {{filename}}（录制 ID {{recording_id}}）未通过审核。\n审核意见：\n{{comments}}", "high", "数据审核打回"),
    ("account_created", "account", "账号已创建", "您的账号 {{username}} 已开通，请使用初始密码登录并立即修改密码。", "medium", "账号创建"),
    ("account_password_reset", "account", "密码已重置", "管理员已重置您的登录密码，请使用新临时密码登录并尽快修改。", "high", "密码重置"),
    ("account_status", "account", "账号状态变更", "{{detail}}", "medium", "启停与注销"),
    ("account_locked", "account", "账号已临时锁定", "连续登录失败次数过多，账号已锁定至 {{locked_until}}。", "high", "登录锁定"),
    ("account_first_login", "account", "请修改初始密码", "为保障安全，首次登录后请先修改初始密码。", "high", "首次改密提醒"),
    ("sys_announcement", "system", "{{title}}", "{{body}}", "medium", "系统公告"),
]


def ensure_default_templates() -> None:
    existing = {t.template_key for t in MessageTemplate.query.all()}
    for key, cat, title_t, body_t, pri, desc in DEFAULT_TEMPLATE_SEEDS:
        if key in existing:
            continue
        db.session.add(
            MessageTemplate(
                template_key=key,
                category=cat,
                title_template=title_t,
                body_template=body_t,
                enabled=True,
                default_priority=pri,
                description=desc,
            )
        )


def _sender_type_for_user(user_id: int | None) -> tuple[str, int | None]:
    if not user_id:
        return "system", None
    u = User.query.get(user_id)
    if not u:
        return "user", user_id
    if u.role == "super_admin":
        return "super_admin", user_id
    if u.role == "admin":
        return "admin", user_id
    return "user", user_id


def _channel_stub_logs(
    inbox_message_id: int,
    recipient_id: int,
    sender_type: str,
    sender_id: int | None,
    msg_type: str,
    title: str,
    body: str,
) -> None:
    cfg = get_or_create_channel_config()
    user = User.query.get(recipient_id)
    role = user.role if user else ""
    try:
        email_roles = json.loads(cfg.email_roles_json or "[]")
    except (json.JSONDecodeError, TypeError):
        email_roles = []
    if cfg.email_enabled:
        status = "skipped"
        detail = "email-gateway-not-configured"
        if email_roles and role not in email_roles:
            status = "skipped"
            detail = "role-not-in-email-scope"
        db.session.add(
            MessageDeliveryLog(
                inbox_message_id=inbox_message_id,
                recipient_id=recipient_id,
                sender_type=sender_type,
                sender_id=sender_id,
                msg_type=msg_type,
                channel="email",
                status=status,
                title_snapshot=title[:500] if title else "",
                body_snapshot=body,
                detail=detail[:500] if detail else None,
            )
        )
    if cfg.sms_enabled:
        db.session.add(
            MessageDeliveryLog(
                inbox_message_id=inbox_message_id,
                recipient_id=recipient_id,
                sender_type=sender_type,
                sender_id=sender_id,
                msg_type=msg_type,
                channel="sms",
                status="skipped",
                title_snapshot=title[:500] if title else "",
                body_snapshot=body,
                detail="sms-gateway-not-configured",
            )
        )


def emit_message(
    recipient_ids: list[int],
    category: str,
    msg_type: str,
    variables: dict | None,
    *,
    title: str | None = None,
    body: str | None = None,
    summary: str | None = None,
    priority: str | None = None,
    sender_id: int | None = None,
    business_type: str | None = None,
    business_id: str | None = None,
    action_url: str | None = None,
    use_template_key: str | None = None,
) -> list[int]:
    """
    写入收件箱 + 站内投递日志；邮件/短信记为 skipped 桩。
    若存在启用模板 use_template_key 或 msg_type，则用模板覆盖 title/body（variables 参与替换）。
    """
    ensure_default_templates()
    variables = variables or {}
    tkey = use_template_key or msg_type
    tmpl = load_template(tkey)
    if tmpl:
        title = render_vars(tmpl.title_template, variables)
        body = render_vars(tmpl.body_template, variables)
        priority = priority or tmpl.default_priority or "medium"
    else:
        title = title or msg_type
        body = body or ""
        priority = priority or "medium"
    summary = summary or (body[:200] + "…" if len(body) > 200 else body)
    sender_type, eff_sender = _sender_type_for_user(sender_id)
    created: list[int] = []
    for rid in {int(x) for x in recipient_ids if x}:
        row = InboxMessage(
            recipient_id=rid,
            sender_type=sender_type,
            sender_id=eff_sender,
            category=category,
            msg_type=msg_type,
            priority=priority,
            title=title[:500],
            summary=(summary or "")[:500],
            body=body,
            business_type=business_type or "none",
            business_id=(business_id or "")[:64],
            action_url=(action_url or "")[:512] if action_url else None,
        )
        db.session.add(row)
        db.session.flush()
        created.append(row.id)
        db.session.add(
            MessageDeliveryLog(
                inbox_message_id=row.id,
                recipient_id=rid,
                sender_type=sender_type,
                sender_id=eff_sender,
                msg_type=msg_type,
                channel="in_app",
                status="sent",
                title_snapshot=row.title,
                body_snapshot=body,
                detail=None,
            )
        )
        _channel_stub_logs(row.id, rid, sender_type, eff_sender, msg_type, row.title, body)
    return created


def task_variables(task: Task | None, task_id: int, extra: dict | None = None) -> dict:
    t = task or Task.query.get(task_id)
    base = {
        "title": t.title if t else "",
        "task_no": str(t.task_no or task_id) if t else str(task_id),
        "end_date": t.end_date.strftime("%Y-%m-%d %H:%M") if t and t.end_date else "",
        "required_count": str(t.required_count) if t else "",
    }
    if extra:
        base.update(extra)
    return base


def emit_task_notification(
    recipient_ids: list[int],
    sender_id: int | None,
    task_id: int,
    title: str,
    body: str,
    msg_type: str,
    priority: str = "medium",
    extra_variables: dict | None = None,
) -> list[int]:
    task = Task.query.get(task_id)
    variables = task_variables(task, task_id, extra_variables)
    return emit_message(
        recipient_ids,
        "task",
        msg_type,
        variables,
        title=title,
        body=body,
        priority=priority,
        sender_id=sender_id,
        business_type="task",
        business_id=str(task_id),
        action_url=f"/tasks?highlight={task_id}",
        use_template_key=msg_type,
    )


def emit_audit_inbox(
    recipient_ids: list[int],
    sender_id: int,
    recording_id: int,
    filename: str,
    comments: str,
) -> list[int]:
    variables = {
        "recording_id": str(recording_id),
        "filename": filename or "",
        "comments": comments or "",
    }
    return emit_message(
        recipient_ids,
        "audit",
        "audit_reject",
        variables,
        sender_id=sender_id,
        business_type="recording",
        business_id=str(recording_id),
        action_url=f"/manage",
        use_template_key="audit_reject",
    )


def emit_account_notification(
    recipient_ids: list[int],
    msg_type: str,
    detail: str,
    *,
    username: str = "",
    locked_until: str = "",
    sender_id: int | None = None,
    priority: str | None = None,
) -> list[int]:
    variables = {"detail": detail, "username": username, "locked_until": locked_until}
    return emit_message(
        recipient_ids,
        "account",
        msg_type,
        variables,
        sender_id=sender_id,
        business_type="user",
        business_id=username or "",
        action_url="/profile",
        use_template_key=msg_type,
        priority=priority,
    )


def _announcement_matches_user(audience: dict, user: User) -> bool:
    mode = audience.get("mode") or "all"
    if mode == "all":
        return True
    if mode == "roles":
        roles = audience.get("roles") or []
        return user.role in roles
    if mode == "departments":
        depts = audience.get("departments") or []
        dept = (user.department or "").strip()
        return dept in depts
    return False


def publish_announcement_fanout(announcement_id: int) -> int:
    """将公告扇出为站内信；返回发送人数。"""
    ann = SystemAnnouncement.query.get(announcement_id)
    if not ann or not ann.is_active:
        return 0
    now = datetime.utcnow()
    if ann.valid_from and ann.valid_from > now:
        return 0
    if ann.valid_until and ann.valid_until < now:
        return 0
    try:
        audience = json.loads(ann.audience_json or "{}")
    except (json.JSONDecodeError, TypeError):
        audience = {"mode": "all"}
    users = User.query.filter_by(is_active=True, is_locked_account=False).all()
    recipients = [u.id for u in users if _announcement_matches_user(audience, u)]
    plain = re.sub(r"<[^>]+>", "", ann.body_html or "")
    emit_message(
        recipients,
        "system",
        "sys_announcement",
        {"title": ann.title, "body": plain},
        title=ann.title,
        body=plain,
        summary=plain[:200],
        priority=ann.priority,
        sender_id=ann.created_by,
        business_type="announcement",
        business_id=str(ann.id),
        action_url="/messages",
        use_template_key="sys_announcement",
    )
    ann.published_at = datetime.utcnow()
    return len(recipients)


def recent_deadline_notice_exists(recipient_id: int, task_id: int, msg_type: str, hours: float = 6) -> bool:
    since = datetime.utcnow() - timedelta(hours=hours)
    q = InboxMessage.query.filter(
        InboxMessage.recipient_id == recipient_id,
        InboxMessage.msg_type == msg_type,
        InboxMessage.business_type == "task",
        InboxMessage.business_id == str(task_id),
        InboxMessage.created_at >= since,
    )
    return db.session.query(q.exists()).scalar()


def scan_task_deadline_warnings() -> dict:
    """扫描未完成任务，发送 24h / 12h 预警（去重）。"""
    from services.task_workflow import effective_workflow_status

    now = datetime.utcnow()
    done_states = ("completed", "archived", "terminated", "draft")
    count_24 = 0
    count_12 = 0
    tasks = Task.query.all()
    for task in tasks:
        ws = effective_workflow_status(task)
        if ws in done_states or not task.end_date:
            continue
        delta = task.end_date - now
        if delta.total_seconds() <= 0:
            continue
        assignees = [r.user_id for r in TaskAssignment.query.filter_by(task_id=task.id).all()]
        if task.created_by and task.created_by not in assignees:
            assignees.append(task.created_by)
        variables = task_variables(task, task.id)
        if delta <= timedelta(hours=12):
            for uid in assignees:
                if recent_deadline_notice_exists(uid, task.id, "task_deadline_12h", hours=5):
                    continue
                emit_task_notification(
                    [uid],
                    None,
                    task.id,
                    "",
                    "",
                    "task_deadline_12h",
                    "urgent",
                    extra_variables=variables,
                )
                count_12 += 1
        elif delta <= timedelta(hours=24):
            for uid in assignees:
                if recent_deadline_notice_exists(uid, task.id, "task_deadline_24h", hours=8):
                    continue
                emit_task_notification(
                    [uid],
                    None,
                    task.id,
                    "",
                    "",
                    "task_deadline_24h",
                    "high",
                    extra_variables=variables,
                )
                count_24 += 1
    return {"warn_24h": count_24, "warn_12h": count_12}


def legacy_task_message_mirror(recipient_ids: list[int], sender_id: int, task_id: int, title: str, body: str) -> None:
    """兼容旧版 task_messages 表（可选同步）。"""
    for rid in {int(x) for x in recipient_ids if x}:
        db.session.add(
            TaskMessage(
                recipient_id=rid,
                sender_id=sender_id or 0,
                task_id=task_id,
                title=title[:255],
                body=body,
            )
        )


# --- notify_task_users：统一走站内信中心 + 可选镜像 TaskMessage ---
def notify_task_users(
    recipient_ids,
    sender_id,
    task_id,
    title,
    body,
    msg_type: str = "task_generic",
    priority: str = "medium",
    extra_variables: dict | None = None,
    mirror_legacy_task_table: bool = True,
):
    ids = emit_task_notification(
        recipient_ids,
        sender_id,
        task_id,
        title,
        body,
        msg_type,
        priority,
        extra_variables=extra_variables,
    )
    if mirror_legacy_task_table:
        sid = sender_id
        if not sid:
            t = Task.query.get(task_id)
            sid = t.created_by if t and t.created_by else None
        if sid:
            legacy_title = title
            legacy_body = body
            task = Task.query.get(task_id)
            if task:
                tmpl = load_template(msg_type)
                if tmpl:
                    vars_ = task_variables(task, task_id, extra_variables)
                    legacy_title = render_vars(tmpl.title_template, vars_)[:255]
                    legacy_body = render_vars(tmpl.body_template, vars_)
            legacy_task_message_mirror(
                list({int(x) for x in recipient_ids if x}),
                int(sid),
                int(task_id),
                legacy_title,
                legacy_body,
            )
    return ids
