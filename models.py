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
    # 反馈#8：账号昵称（界面显示名优先取它，如超管 GR）；管理员备注（可空）
    nickname = db.Column(db.String(80))
    department = db.Column(db.String(128))
    phone = db.Column(db.String(32))
    admin_remark = db.Column(db.Text)  # 管理员对账号的备注说明（可空）
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


# 注：登录/账号审计/导出等运维日志统一并入 system_event_logs（SystemEventLog，见文件末尾），
# 原 login_logs / user_audit_logs / export_logs 三表已退役（迁移脚本 RENAME 备份）。


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

    # ---- 采集元数据（表精简：原 acquisition_metadata 表并入 recordings）----
    uploader_id = db.Column(db.Integer, db.ForeignKey('users.id'), index=True)  # 上传人
    file_type = db.Column(db.String(20), index=True)  # video / image / audio
    file_size = db.Column(db.BigInteger)  # bytes
    file_md5 = db.Column(db.String(32), index=True)  # 同文件去重 / 算法结果复用
    source_channel = db.Column(db.String(20), default='upload')  # upload/record/crawl
    acquisition_task_no = db.Column(db.String(64), index=True)  # 采集时填写的任务编号
    acquisition_audit_status = db.Column(db.String(20), default='pending', index=True)  # pending/pass/pending_fix

    # ---- 反馈#6：垃圾箱（软删除，保留 30 天可恢复，到期物理删除）----
    deleted_at = db.Column(db.DateTime, index=True)
    deleted_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    # 审核员对文件的备注说明（随文件归档，数据管理中可见）
    review_remark = db.Column(db.Text)

    # ---- 反馈#8：互联网批量采集扩展 ----
    crawl_remark = db.Column(db.Text)  # 采集内容备注（说明该视频/图片主要是做什么的）
    crawl_meta_json = db.Column(db.Text)  # 采集元数据 JSON（来源站点/播放量/弹幕/UP主/采集选项等）
    meta_json_path = db.Column(db.String(512))  # 随媒体落盘的 .meta.json 侧车文件路径

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
    # f3：同一任务同一用户仅一条分配记录（防并发申领/重复分配）
    __table_args__ = (
        db.UniqueConstraint('task_id', 'user_id', name='uq_task_assignment_task_user'),
    )
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
    """自定义数据集（命名、版本、描述 + 反馈#15：输出格式 / 导出内容 / 训练集划分）。"""
    __tablename__ = 'data_sets'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    version_label = db.Column(db.String(32), default='V1.0')
    # 反馈#15：导出配置——output_format: jsonl/json/csv/zip_media；
    # include_fields: 逗号分隔（media,transcript,timeline,metadata,audit）；split_rule: none/8:1:1/7:1:2/9:0.5:0.5
    output_format = db.Column(db.String(16), default='jsonl')
    include_fields = db.Column(db.String(255), default='media,transcript,metadata')
    split_rule = db.Column(db.String(16), default='none')
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


class AuditLog(db.Model):
    """全链路审核操作留痕：通过(pass) / 打回修正(reject_return) / 审核员自行修正(self_fix)；
    表精简后原 inspections 表的采集审核记录并入本表（action_type='inspection'，corrections 存 detail_json）。"""
    __tablename__ = 'audit_logs'
    id = db.Column(db.Integer, primary_key=True)
    recording_id = db.Column(db.Integer, db.ForeignKey('recordings.id'), nullable=False, index=True)
    auditor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    action_type = db.Column(db.String(32), nullable=False)  # pass / reject_return / self_fix / inspection
    audit_result = db.Column(db.String(32), nullable=False)  # approved / pending_fix / rejected
    comments = db.Column(db.Text)
    detail_json = db.Column(db.Text)  # 结构化：时间轴快照、修正摘要、原 inspections.corrections 等
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


# 注：原 message_channel_config 单例表已退役——邮件/短信渠道开关改为环境变量
# （MSG_EMAIL_ENABLED / MSG_SMS_ENABLED / MSG_EMAIL_ROLES），由 services/messaging.py 读取。


# ---------------- 算力积分（按人计量 DeepSeek API 与 GPU 算力消耗）----------------

class UserCreditGrant(db.Model):
    """积分分配记录：管理员按人发放，追加式流水（Redis 余额实时累加，本表为持久凭据）。"""
    __tablename__ = 'user_credit_grants'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    credits = db.Column(db.Integer, nullable=False)  # 本次发放积分（正数）
    period = db.Column(db.String(16), default='permanent', index=True)  # 周期标签：202609 / permanent
    reason = db.Column(db.String(255))
    granted_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class ComputeUsageLog(db.Model):
    """算力消耗流水：每次算法推理一条；MySQL 为持久账单，Redis 计数为实时防刷。"""
    __tablename__ = 'compute_usage_logs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    backend = db.Column(db.String(16), nullable=False, index=True)  # autodl / local / deepseek / cache
    task_id = db.Column(db.String(64), index=True)  # Redis 队列任务 UUID
    recording_id = db.Column(db.Integer, db.ForeignKey('recordings.id'), index=True)
    modality = db.Column(db.String(16))
    metric_type = db.Column(db.String(24))  # tokens / media_seconds / cache_hit
    metric_value = db.Column(db.Float, default=0)  # token 数 / 媒体秒数
    cost_credits = db.Column(db.Integer, default=0)  # 实际扣减积分
    est_credits = db.Column(db.Integer, default=0)  # 提交时冻结积分
    status = db.Column(db.String(16), default='success', index=True)  # success / failed / reused
    detail = db.Column(db.String(512))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


# ---------------- 系统核心配置（键值表，超级管理员运行时管理，如 DeepSeek API Key）----------------

class SystemSetting(db.Model):
    """系统核心配置键值表（反馈#7）：DeepSeek API Key/Base URL/模型/配额等运行时可改配置。
    属"系统核心配置"，仅超级管理员可读写；启动时加载并覆盖环境变量默认值。"""
    __tablename__ = 'system_settings'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(64), unique=True, nullable=False, index=True)
    value = db.Column(db.Text)
    remark = db.Column(db.String(255))
    updated_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ---------------- 统一系统事件日志（登录/账号审计/导出等运维审计，合并自多张旧日志表）----------------

class SystemEventLog(db.Model):
    """统一系统事件日志：event_type 区分 login / user_audit / export 等，替代分散的旧日志表。"""
    __tablename__ = 'system_event_logs'
    id = db.Column(db.Integer, primary_key=True)
    event_type = db.Column(db.String(32), nullable=False, index=True)  # login / user_audit / export ...
    actor_id = db.Column(db.Integer, db.ForeignKey('users.id'), index=True)
    target_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), index=True)
    success = db.Column(db.Boolean, default=True)
    ip_address = db.Column(db.String(45))
    action_type = db.Column(db.String(64), index=True)  # 事件子类：login_success / export_dataset ...
    summary = db.Column(db.String(255))
    detail_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
