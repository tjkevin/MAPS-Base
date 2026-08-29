from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin

db = SQLAlchemy()

class User(db.Model, UserMixin):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    # super_admin | admin | recorder | inspector
    role = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    full_name = db.Column(db.String(80))
    department = db.Column(db.String(128))
    phone = db.Column(db.String(32))
    is_active = db.Column(db.Boolean, default=True)
    is_locked_account = db.Column(db.Boolean, default=False)
    account_valid_until = db.Column(db.DateTime)
    must_change_password = db.Column(db.Boolean, default=False)
    failed_login_count = db.Column(db.Integer, default=0)
    locked_until = db.Column(db.DateTime)
    session_version = db.Column(db.Integer, default=0)
    last_login_at = db.Column(db.DateTime)
    last_login_ip = db.Column(db.String(45))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class LoginLog(db.Model):
    """登录/登出审计（含失败尝试）。"""
    __tablename__ = 'login_logs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), index=True)
    username_attempted = db.Column(db.String(80), index=True)
    event_type = db.Column(db.String(20), nullable=False, index=True)  # login_success / login_fail / logout / idle_timeout / force_logout
    success = db.Column(db.Boolean, default=False)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(512))
    message = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class UserAuditLog(db.Model):
    """账号与权限类操作审计。"""
    __tablename__ = 'user_audit_logs'
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    target_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), index=True)
    action_type = db.Column(db.String(40), nullable=False, index=True)
    detail_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class Recording(db.Model):
    __tablename__ = 'recordings'
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(512), nullable=False)
    duration = db.Column(db.Float)  # Duration in seconds
    recorded_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    recorded_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20))  # pending, processing, completed, rejected, pending_review, pending_fix, approved
    text_content = db.Column(db.Text)
    task_type = db.Column(db.Integer)  # 1 for first type, 2 for second type
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # 字幕相关字段
    subtitle_srt_path = db.Column(db.String(512))  # SRT字幕文件路径
    subtitle_json_path = db.Column(db.String(512))  # JSON字幕文件路径
    subtitle_content = db.Column(db.Text)  # 字幕内容
    subtitle_json_data = db.Column(db.Text)  # JSON格式的字幕数据
    # 逻辑删除（无效数据）：不删物理文件，仅标记可追溯
    invalidated_at = db.Column(db.DateTime, index=True)
    invalidated_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    business_task_id = db.Column(db.Integer, db.ForeignKey('tasks.id'), index=True)


class AcquisitionMetadata(db.Model):
    __tablename__ = 'acquisition_metadata'
    id = db.Column(db.Integer, primary_key=True)
    recording_id = db.Column(db.Integer, db.ForeignKey('recordings.id'), nullable=False, index=True)
    uploader_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    filename = db.Column(db.String(255), nullable=False)
    file_type = db.Column(db.String(20), nullable=False)  # video/image/audio
    file_size = db.Column(db.BigInteger, nullable=False)  # bytes
    file_md5 = db.Column(db.String(32), nullable=False, index=True)
    task_no = db.Column(db.String(64), nullable=False, index=True)
    audit_status = db.Column(db.String(20), default='pending', index=True)  # pending/pass/pending_fix
    source_channel = db.Column(db.String(20), nullable=False, default='upload')  # upload/record/crawl

class Inspection(db.Model):
    __tablename__ = 'inspections'
    id = db.Column(db.Integer, primary_key=True)
    recording_id = db.Column(db.Integer, db.ForeignKey('recordings.id'))
    inspector_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    inspection_date = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20))  # 'approved', 'rejected'
    corrections = db.Column(db.Text)
    comments = db.Column(db.Text)

class Task(db.Model):
    __tablename__ = 'tasks'
    id = db.Column(db.Integer, primary_key=True)
    task_no = db.Column(db.String(64), unique=True, index=True, nullable=True)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    acceptance_criteria = db.Column(db.Text)
    start_date = db.Column(db.DateTime, nullable=False)
    end_date = db.Column(db.DateTime, nullable=False)
    required_count = db.Column(db.Integer, default=0)
    task_type = db.Column(db.Integer)  # 兼容旧版 1/2
    priority = db.Column(db.String(16), default='medium', index=True)  # high / medium / low
    task_category = db.Column(db.String(20), default='process', index=True)  # collect / process / audit
    task_subtype = db.Column(db.String(40))  # upload, record, crawl, transcribe, describe 等
    assign_mode = db.Column(db.String(20), default='manual')  # auto / manual / pool
    max_claim_per_user = db.Column(db.Integer, default=1)
    workflow_status = db.Column(db.String(40), default='draft', index=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    published_at = db.Column(db.DateTime)
    status = db.Column(db.String(20))  # 兼容：active / completed / expired，与工作流同步
    pause_reason = db.Column(db.Text)
    terminate_reason = db.Column(db.Text)
    paused_at = db.Column(db.DateTime)
    terminated_at = db.Column(db.DateTime)
    quality_score = db.Column(db.Float)
    scored_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    scored_at = db.Column(db.DateTime)
    review_comments = db.Column(db.Text)


class TaskAssignment(db.Model):
    __tablename__ = 'task_assignments'
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('tasks.id'), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), index=True)
    assigned_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_count = db.Column(db.Integer, default=0)
    target_quota = db.Column(db.Integer, default=0)
    claimed_from_pool = db.Column(db.Boolean, default=False)
    status = db.Column(db.String(20), default='assigned')


class TaskWorkflowLog(db.Model):
    """任务全生命周期操作日志（审计，不可改库内内容由应用保证只增）。"""
    __tablename__ = 'task_workflow_logs'
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('tasks.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    action_type = db.Column(db.String(40), nullable=False, index=True)
    from_status = db.Column(db.String(40))
    to_status = db.Column(db.String(40))
    detail_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class TaskReturnRequest(db.Model):
    """公海任务退领申请。"""
    __tablename__ = 'task_return_requests'
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('tasks.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    reason = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default='pending', index=True)  # pending / approved / rejected
    reviewed_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    reviewed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class TaskMessage(db.Model):
    """任务相关站内通知（可对接统一消息中心）。"""
    __tablename__ = 'task_messages'
    id = db.Column(db.Integer, primary_key=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    task_id = db.Column(db.Integer, db.ForeignKey('tasks.id'), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=False)
    read_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class ProcessingResult(db.Model):
    __tablename__ = 'processing_results'
    id = db.Column(db.Integer, primary_key=True)
    recording_id = db.Column(db.Integer, db.ForeignKey('recordings.id'), nullable=False, index=True)
    processor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    modality = db.Column(db.String(20), nullable=False)  # audio / video
    process_mode = db.Column(db.String(20), nullable=False, default='manual')  # manual / algorithm / reviewer_self_fix
    content_text = db.Column(db.Text)  # 转写文本 / 视频描述文本
    timeline_json = db.Column(db.Text)  # 时间轴结构化JSON
    algorithm_raw_json = db.Column(db.Text)  # 算法原始返回（可选）
    processed_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class FilterTemplate(db.Model):
    """用户保存的常用筛选条件（JSON）。"""
    __tablename__ = 'filter_templates'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    name = db.Column(db.String(128), nullable=False)
    criteria_json = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class DataSet(db.Model):
    """自定义数据集（命名、版本、描述）。"""
    __tablename__ = 'data_sets'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    version_label = db.Column(db.String(32), default='V1.0')
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DataSetItem(db.Model):
    __tablename__ = 'data_set_items'
    id = db.Column(db.Integer, primary_key=True)
    dataset_id = db.Column(db.Integer, db.ForeignKey('data_sets.id'), nullable=False, index=True)
    recording_id = db.Column(db.Integer, db.ForeignKey('recordings.id'), nullable=False, index=True)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('dataset_id', 'recording_id', name='uq_dataset_recording'),)


class ExportLog(db.Model):
    """结构化导出审计日志。"""
    __tablename__ = 'export_logs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    scope_type = db.Column(db.String(32), nullable=False)  # filter / dataset / ids
    scope_ref = db.Column(db.String(255))
    export_format = db.Column(db.String(16), nullable=False)
    fields_json = db.Column(db.Text)
    row_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class AuditLog(db.Model):
    """全链路审核操作留痕：通过 / 打回修正 / 审核员自行修正。"""
    __tablename__ = 'audit_logs'
    id = db.Column(db.Integer, primary_key=True)
    recording_id = db.Column(db.Integer, db.ForeignKey('recordings.id'), nullable=False, index=True)
    auditor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    action_type = db.Column(db.String(32), nullable=False)  # pass / reject_return / self_fix
    audit_result = db.Column(db.String(32), nullable=False)  # approved / pending_fix
    comments = db.Column(db.Text)
    detail_json = db.Column(db.Text)  # 结构化：时间轴快照、修正摘要等
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class AuditMessage(db.Model):
    """审核打回时推送给原处理人的站内消息（消息管理模块可对接扩展）。"""
    __tablename__ = 'audit_messages'
    id = db.Column(db.Integer, primary_key=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    recording_id = db.Column(db.Integer, db.ForeignKey('recordings.id'), nullable=True, index=True)
    title = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=False)
    read_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class InboxMessage(db.Model):
    """统一站内信收件箱（任务/审核/账号/系统）；逻辑删除仅影响个人列表。"""
    __tablename__ = 'inbox_messages'
    id = db.Column(db.Integer, primary_key=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    sender_type = db.Column(db.String(24), nullable=False, default='system')  # system | super_admin | admin | user
    sender_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    category = db.Column(db.String(20), nullable=False, index=True)  # task | audit | account | system
    msg_type = db.Column(db.String(64), nullable=False, index=True)
    priority = db.Column(db.String(16), nullable=False, default='medium', index=True)  # urgent | high | medium | low
    title = db.Column(db.String(512), nullable=False)
    summary = db.Column(db.String(512))
    body = db.Column(db.Text, nullable=False)
    business_type = db.Column(db.String(32), index=True)  # task | recording | user | announcement | none
    business_id = db.Column(db.String(64), index=True)
    action_url = db.Column(db.String(512))
    read_at = db.Column(db.DateTime, index=True)
    pinned_at = db.Column(db.DateTime, index=True)
    user_deleted_at = db.Column(db.DateTime, index=True)
    extra_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class MessageDeliveryLog(db.Model):
    """消息投递审计（站内/邮件/短信）；只追加。"""
    __tablename__ = 'message_delivery_logs'
    id = db.Column(db.Integer, primary_key=True)
    inbox_message_id = db.Column(db.Integer, db.ForeignKey('inbox_messages.id'), index=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    sender_type = db.Column(db.String(24), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    msg_type = db.Column(db.String(64), nullable=False, index=True)
    channel = db.Column(db.String(16), nullable=False, index=True)  # in_app | email | sms
    status = db.Column(db.String(16), nullable=False, index=True)  # sent | failed | skipped | pending
    title_snapshot = db.Column(db.String(512))
    body_snapshot = db.Column(db.Text)
    detail = db.Column(db.String(512))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class MessageTemplate(db.Model):
    """可配置消息模板（变量 {{变量名}}）。"""
    __tablename__ = 'message_templates'
    id = db.Column(db.Integer, primary_key=True)
    template_key = db.Column(db.String(64), unique=True, nullable=False, index=True)
    category = db.Column(db.String(20), nullable=False)
    title_template = db.Column(db.String(512), nullable=False)
    body_template = db.Column(db.Text, nullable=False)
    enabled = db.Column(db.Boolean, default=True)
    default_priority = db.Column(db.String(16), default='medium')
    description = db.Column(db.String(255))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SystemAnnouncement(db.Model):
    """系统公告（管理员发布，可设有效期与接收范围）。"""
    __tablename__ = 'system_announcements'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    body_html = db.Column(db.Text, nullable=False)
    priority = db.Column(db.String(16), nullable=False, default='medium')
    valid_from = db.Column(db.DateTime)
    valid_until = db.Column(db.DateTime)
    audience_json = db.Column(db.Text, nullable=False)  # {"mode":"all"|"roles"|"departments","roles":[],"departments":[]}
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    published_at = db.Column(db.DateTime, index=True)
    is_active = db.Column(db.Boolean, default=True, index=True)


class MessageChannelConfig(db.Model):
    """可选渠道开关（单例 id=1）；邮件/短信为扩展桩。"""
    __tablename__ = 'message_channel_config'
    id = db.Column(db.Integer, primary_key=True)
    email_enabled = db.Column(db.Boolean, default=False)
    sms_enabled = db.Column(db.Boolean, default=False)
    email_roles_json = db.Column(db.Text)  # JSON list of roles that may receive email
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
