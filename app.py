from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    send_file,
    send_from_directory,
    Response,
    redirect,
    url_for,
    session,
    flash,
    after_this_request,
)
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.utils import secure_filename
import os
import time
import hashlib
import hmac
import secrets
import uuid
import re
import random
import importlib.util
import json
from datetime import datetime, timedelta
from sqlalchemy import func, or_
from models import (
    db,
    User,
    Recording,
    Task,
    TaskAssignment,
    TaskWorkflowLog,
    TaskReturnRequest,
    ProcessingResult,
    AuditLog,
    InboxMessage,
    MessageDeliveryLog,
    MessageTemplate,
    SystemAnnouncement,
    FilterTemplate,
    DataSet,
    DataSetItem,
    UserCreditGrant,
    ComputeUsageLog,
    SystemEventLog,
    SystemSetting,
)
from config import Config
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from services.bagel_queue import BagelQueueService
from services.model_backends import (
    BACKEND_DEEPSEEK,
    BACKEND_AUTODL,
    BACKEND_LOCAL,
    FatalBackendError,
    QuotaExceeded,
    build_file_download_url,
    call_deepseek,
    deepseek_quota_status,
    gpu_backend_health,
    gpu_protocol,
    resolve_capable_gpu_target,
    resolve_capability_target,
    select_backend,
    TASK_KIND_TRANSCRIBE,
    CAP_TRANSCRIBE,
    CAP_VIDEO_KEYFRAMES,
    PROTOCOL_LAN,
    IMAGE_ONLY_PROTOCOLS,
)
from services import algorithm_prompts
from services import web_crawler
from services import credits as credit_service
from services.credits import CreditInsufficient
import redis as _redis_lib
from services.task_workflow import (
    CATEGORY_ROLE,
    WORKFLOW_LABELS,
    effective_workflow_status,
    sync_legacy_status,
    log_task_action,
    notify_task_users,
    total_completed_for_task,
    total_quota_for_task,
    validate_task_config,
    can_create_task,
    is_task_creator_or_admin,
    eligible_users_for_category,
    auto_assign,
    serialize_task_summary,
)
from services.auth_security import (
    MAX_FAILED_ATTEMPTS,
    LOCK_MINUTES,
    validate_password_strength,
    generate_initial_password,
)
from services.data_management import (
    normalize_manage_args,
    manage_records_base_query,
    serialize_recording_row,
    build_export_rows,
    export_as_response,
    compute_dashboard_stats,
    create_batch_zip,
    detect_file_type as dm_detect_file_type,
)

app = Flask(__name__)
app.config.from_object(Config)
_db_uri = str(app.config.get('SQLALCHEMY_DATABASE_URI') or '')
if 'mysql' in _db_uri and 'pymysql' in _db_uri:
    try:
        import pymysql  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            '当前 SQLALCHEMY_DATABASE_URI 使用 MySQL（mysql+pymysql），但未安装 PyMySQL。'
            '请在**当前运行 app 的 Python 环境**执行：pip install PyMySQL\n'
            '若项目自带 venv，请先激活 venv（例如 .\\venv\\Scripts\\activate）再启动，或运行：'
            'venv\\Scripts\\python.exe app.py'
        ) from e
db.init_app(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
bagel_queue = BagelQueueService(
    redis_url=app.config["REDIS_URL"],
    queue_name=app.config["BAGEL_QUEUE_NAME"],
    task_key_prefix=app.config["BAGEL_TASK_KEY_PREFIX"],
    result_ttl_seconds=app.config["BAGEL_RESULT_TTL_SECONDS"],
    delayed_zset=app.config.get("BAGEL_DELAYED_ZSET", "bagel:task:delayed"),
    dead_queue_name=app.config.get("BAGEL_DEAD_QUEUE_NAME", "bagel:task:dead"),
    worker_prefix=app.config.get("BAGEL_WORKER_PREFIX", "bagel:worker:"),
)
# Redis 直连客户端（DeepSeek 配额计数/查询）
model_redis = _redis_lib.Redis.from_url(app.config["REDIS_URL"], decode_responses=True)

# 登录限流：INCR + EXPIRE 用 Lua 原子化，避免 INCR 后进程崩溃导致键无 TTL 永久限流
_LOGIN_RL_LUA = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return {n, redis.call('TTL', KEYS[1])}
"""
_login_rl_script = model_redis.register_script(_LOGIN_RL_LUA)


def _client_ip():
    """
    取真实客户端 IP（供限流与审计使用）：
    - nginx 反代固定设置 X-Real-IP（可信，优先）；
    - X-Forwarded-For 最左值可被客户端任意伪造，仅作回退；
    - 再回退 remote_addr。
    注：5000 端口不对公网暴露，公网流量均经 nginx，X-Real-IP 可信。
    """
    ip = (request.headers.get('X-Real-IP') or '').strip()
    if not ip:
        xff = (request.headers.get('X-Forwarded-For') or '').strip()
        if xff:
            ip = xff.split(',')[0].strip()
    return ip or request.remote_addr or 'unknown'


# ---- f4：统一安全响应头（所有响应生效）----
@app.after_request
def _set_security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    resp.headers.setdefault('Referrer-Policy', 'same-origin')
    # 项目使用内联脚本/样式 + 三个固定 CDN；media/blob 供播放器与本地预览
    resp.headers.setdefault(
        'Content-Security-Policy',
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://code.jquery.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "font-src 'self' data: https://cdnjs.cloudflare.com; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'self'"
    )
    return resp


# ---- f4：轻量 CSRF 令牌（会话绑定，用于表单类状态变更如 logout）----
def _csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_hex(16)
        session['_csrf_token'] = token
    return token


def _csrf_valid(token):
    expected = session.get('_csrf_token')
    return bool(expected) and bool(token) and hmac.compare_digest(str(token), str(expected))


@app.context_processor
def _inject_csrf():
    return {'csrf_token': _csrf_token}


def _build_algorithm_payload(recording, data):
    """构建算法任务 payload（两个提交端点共用），含后端路由与外部文件下载URL。"""
    payload = {
        'recording_id': recording.id,
        'file_path': recording.file_path,
        'filename': recording.filename,
        'modality': detect_modality(recording.filename),
        'submitted_by': current_user.id,
        'options': data.get('options', {}) or {},
        # backend 可选：autodl（BAGEL满血版）/ local（BAGEL优化版）/ deepseek / auto；留空用系统默认
        'backend': (data.get('backend') or '').strip().lower(),
        # 如需指定本次任务的推理服务地址可传 bagel_service_url 覆盖（一般留空，按 backend 选端点）
        'bagel_service_url': (data.get('bagel_service_url') or '').strip(),
    }
    # 外部 worker（autoDL/局域网）凭此 URL 下载云端文件
    file_url = build_file_download_url(payload, Config)
    if file_url:
        payload['file_download_url'] = file_url

    # 反馈#10：Whisper 音/视频转写任务（可附带关键帧简述、外部大模型详述）走独立预检分支
    opts = payload.get('options') or {}
    if str(opts.get('task_kind') or '').strip().lower() == TASK_KIND_TRANSCRIBE:
        return _build_transcribe_payload(recording, payload, opts)

    # DeepSeek 后端：入队前做配额/配置预检，提前给出明确错误（未配置 API_KEY 同样在此拦截，
    # 避免任务入队后才由 worker 判不可重试错误入死信）
    backend = select_backend(payload, Config.MODEL_DEFAULT_BACKEND,
                             getattr(Config, 'MODEL_DEFAULT_GPU_BACKEND', 'local'))
    # 反馈#8：图片理解只能走 BAGEL（autoDL/局域网 GPU）；外部大模型为纯文本接口，无法看图
    if payload.get('modality') == 'image' and backend == BACKEND_DEEPSEEK:
        raise FatalBackendError('图片理解任务需使用 BAGEL 后端（autoDL 满血版 / 局域网优化版），外部大模型为纯文本接口不支持图片')
    # 反馈#8续：能力感知预检——图片-only 协议端点（局域网 NF4 优化版 lan、autoDL SFT VQA
    # 服务 autodl_v1）均仅支持图片理解；音视频理解任务若最终只能落到这类端点，入队后必
    # 失败入死信，故在此提前给出明确提示（音/视频请改用「AI 转写」：Whisper + 关键帧简述）
    if backend != BACKEND_DEEPSEEK:
        gpu_target, _ = resolve_capable_gpu_target(backend, payload, Config, model_redis)
        if gpu_protocol(gpu_target, Config) in IMAGE_ONLY_PROTOCOLS and payload.get('modality') != 'image':
            raise FatalBackendError(
                '音/视频的算法理解请使用「AI 转写」任务（Whisper 转写 + BAGEL 关键帧简述，'
                '需局域网 BAGEL 优化版端点）；当前可用的 GPU 端点（局域网 NF4 优化版 / '
                'autoDL SFT 模型服务）均仅支持单图理解。请联系管理员确认局域网 GPU 机的 '
                '转写服务已启动，或对图片使用算法理解。'
            )
        # 反馈#9：GPU 端点连通性预检——端点不健康（模型机未开机/隧道未连通/模型加载失败）时
        # 即时返回明确提示，避免任务入队后重试耗尽入死信；模型加载中(loading)视为健康，任务会等其就绪后执行
        gpu_ok, gpu_detail = gpu_backend_health(gpu_target, Config, model_redis)
        if not gpu_ok:
            label = 'autoDL BAGEL 满血版' if gpu_target == BACKEND_AUTODL else '局域网 BAGEL 优化版'
            raise FatalBackendError(
                f'{label}当前不可用（{gpu_detail}）。请确认模型服务已启动、网络隧道已连通后重试；'
                '音视频任务需配置 autoDL 满血版端点。'
            )
    if backend == BACKEND_DEEPSEEK:
        from services.model_backends import _check_quota
        _check_quota(model_redis, Config)
        # 反馈#7：DeepSeek 默认提示词（处理环节）——未显式传 messages/system_prompt/prompt 时注入
        opts = payload.get('options') or {}
        if not (opts.get('messages') or opts.get('system_prompt') or opts.get('prompt')):
            pr = _latest_processing_result(recording.id)
            tl = []
            if pr and pr.timeline_json:
                try:
                    tl = json.loads(pr.timeline_json)
                except (json.JSONDecodeError, TypeError):
                    tl = []
            opts['system_prompt'] = algorithm_prompts.DEEPSEEK_PROCESS_SYSTEM_PROMPT
            opts['prompt'] = algorithm_prompts.build_process_prompt(
                text=recording.text_content or '',
                timeline=tl,
                task_type=recording.task_type or 2,
                extra=opts.get('prompt_extra') or data.get('prompt_extra') or '',
            )
            payload['options'] = opts
    return payload, backend


def _build_transcribe_payload(recording, payload, opts):
    """
    反馈#10：Whisper 转写任务（音/视频）payload 规整与提交前预检。
    - 仅音频/视频可转写；图片不支持；
    - 转写与关键帧能力仅 lan 协议端点具备（GPU 机 bagel_api.py 新端点，需 ffmpeg + faster-whisper）；
    - with_keyframes（BAGEL 关键帧简述）/ with_llm_summary（外部大模型详述）仅视频生效；
      with_llm_summary 额外做外部大模型配额/配置预检；
    - 不注入图片/文本理解的 DeepSeek 处理提示词（worker 内使用专用视频详述 prompt）。
    返回 (payload, 实际 GPU 目标后端)。
    """
    modality = payload.get('modality')
    if modality not in ('audio', 'video'):
        raise FatalBackendError(
            f'AI 转写（Whisper）仅支持音频/视频文件（当前文件类型：{modality or "未知"}）。'
        )

    # 规整选项：task_kind 固定；视频附加项仅对视频生效
    options = dict(opts or {})
    options['task_kind'] = TASK_KIND_TRANSCRIBE
    is_video = modality == 'video'
    options['with_keyframes'] = bool(options.get('with_keyframes')) and is_video
    options['with_llm_summary'] = bool(options.get('with_llm_summary')) and is_video
    lang = str(options.get('whisper_language') or '').strip()
    if lang:
        options['whisper_language'] = lang[:8].lower()
    else:
        options.pop('whisper_language', None)
    payload['options'] = options

    # 后端选择：转写只能走 GPU（lan 新端点）；显式选 deepseek/auto/留空时回落默认 GPU
    preferred = (payload.get('backend') or '').strip().lower()
    if preferred not in (BACKEND_LOCAL, BACKEND_AUTODL):
        preferred = getattr(Config, 'MODEL_DEFAULT_GPU_BACKEND', BACKEND_LOCAL)
    payload['backend'] = preferred

    # 能力预检（强制刷新健康缓存）：找不到支持 transcribe 的健康 lan 端点即明确报错，
    # 避免任务入队后重试耗尽入死信（模型加载中 loading 视为健康，任务排队等待执行）
    target = resolve_capability_target(
        CAP_TRANSCRIBE, preferred, Config, model_redis, force_refresh=True)
    if options['with_keyframes']:
        # 关键帧优先与转写同端点（同一 GPU 机串行执行，避免大文件重复上传）；
        # 该端点缺 ffmpeg 时能力路由会自动尝试另一个已配置的 lan 端点
        resolve_capability_target(
            CAP_VIDEO_KEYFRAMES, target, Config, model_redis, force_refresh=True)
    if options['with_llm_summary']:
        # 外部大模型配额/配置预检（未配置 API_KEY 或超管理员日/月限额时提前拦截）
        from services.model_backends import _check_quota
        _check_quota(model_redis, Config)
    return payload, target


def _recording_file_md5(recording):
    """取录制文件的 MD5（表精简后直接读 recordings.file_md5）。"""
    return recording.file_md5 or None


def _find_reusable_algorithm_result(recording, require_timeline=False):
    """
    同文件（MD5 一致）已有处理结果时直接复用，不再消耗 GPU/API 算力。
    返回可直接作为任务结果的 dict（含 reused 标记），无命中返回 None。
    - require_timeline=False（图片/文本理解）：仅复用 process_mode='algorithm' 的算法结果；
    - require_timeline=True（反馈#10 转写）：算法结果或人工草稿均可，但必须含非空时间轴，
      否则无法填入时间轴编辑器，视为无命中（转写任务不做无意义复用）。
    """
    md5 = _recording_file_md5(recording)
    if not md5:
        return None
    recs = Recording.query.filter_by(file_md5=md5).all()
    rids = [r.id for r in recs]
    if not rids:
        return None
    query = (
        ProcessingResult.query
        .filter(ProcessingResult.recording_id.in_(rids))
        .order_by(ProcessingResult.processed_at.desc())
    )
    if require_timeline:
        # 转写复用：优先算法结果，其次同文件人工转录/校对草稿（含非空时间轴即可复用）
        query = query.filter(ProcessingResult.process_mode.in_(['algorithm', 'manual']))
    else:
        query = query.filter(ProcessingResult.process_mode == 'algorithm')
    pr = query.first()
    if not pr:
        return None
    timeline = []
    if pr.timeline_json:
        try:
            timeline = json.loads(pr.timeline_json)
        except (json.JSONDecodeError, TypeError):
            timeline = []
    if require_timeline and not timeline:
        return None
    reused = {
        'content_text': pr.content_text or '',
        'timeline': timeline,
        'reused': True,
        'source_recording_id': pr.recording_id,
        'backend': 'cache',
        'message': '检测到相同文件(MD5)已完成处理，自动复用历史结果，不消耗算力积分',
    }
    # 转写复用：若历史算法结果中带有视频简述/外部大模型详述，一并带回展示
    if require_timeline and pr.algorithm_raw_json:
        try:
            raw = json.loads(pr.algorithm_raw_json)
            if raw.get('video_summary'):
                reused['video_summary'] = raw['video_summary']
            if raw.get('llm_summary'):
                reused['llm_summary'] = raw['llm_summary']
        except (json.JSONDecodeError, TypeError):
            pass
    return reused


def _submit_algorithm_task(recording, data):
    """
    算法任务提交通道（两个提交端点共用）：
    1) MD5 命中 → 直接生成已完成的复用任务（免费）；
    2) 否则按后端预估积分并冻结（余额不足 429）→ 入队。
    返回 (task_id, backend_label, reused: bool)。
    """
    opts = data.get('options') or {}
    is_transcribe = str(opts.get('task_kind') or '').strip().lower() == TASK_KIND_TRANSCRIBE

    # 反馈#10：转写任务先查 MD5 复用——命中缓存时免费，且无需 GPU 在线/隧道连通；
    # 复用结果必须含非空时间轴（require_timeline），否则无法填入时间轴编辑器
    if is_transcribe and not (data.get('force_recompute') or data.get('force')):
        reused = _find_reusable_algorithm_result(recording, require_timeline=True)
        if reused:
            mini_payload = {
                'recording_id': recording.id,
                'filename': recording.filename,
                'modality': detect_modality(recording.filename),
                'submitted_by': current_user.id,
                'options': {'task_kind': TASK_KIND_TRANSCRIBE},
                'backend': 'cache',
            }
            task_id = bagel_queue.enqueue_reused(mini_payload, reused)
            return task_id, 'cache', True

    payload, backend = _build_algorithm_payload(recording, data)

    if not is_transcribe and not (data.get('force_recompute') or data.get('force')):
        reused = _find_reusable_algorithm_result(recording)
        if reused:
            task_id = bagel_queue.enqueue_reused(payload, reused)
            return task_id, 'cache', True

    # 积分预检 + 冻结
    est_cost, _, _ = credit_service.estimate_cost(backend, recording, payload, Config)
    billing = credit_service.freeze(current_user.id, est_cost, Config, model_redis)
    try:
        task_id = bagel_queue.enqueue(payload, extra={
            'billing': billing,
            'est_cost': est_cost,
            'submitted_by': current_user.id,
            'backend_selected': backend,
        })
    except Exception:
        if billing == 'held':
            credit_service.refund(current_user.id, est_cost, model_redis)
        raise
    return task_id, backend, False


def _settle_algorithm_billing(task):
    """轮询/确认时触发积分结算（失败不影响主流程，下轮轮询重试）。"""
    try:
        result = credit_service.settle_task(
            task, Config, model_redis, app.config['BAGEL_TASK_KEY_PREFIX']
        )
        return result
    except Exception as e:
        app.logger.warning('credit settle failed: %s', e)
        return {'settle_error': str(e)}


def _is_system_admin():
    return current_user.is_authenticated and current_user.role in ('admin', 'super_admin')


def _is_super_admin():
    return current_user.is_authenticated and current_user.role == 'super_admin'


# ---------------- 反馈#7：系统核心配置（SystemSetting 键值表，超级管理员运行时管理） ----------------
# DeepSeek 配置项：DB 键名 -> Config 运行时属性
_DEEPSEEK_SETTING_MAP = {
    'deepseek_api_key': 'DEEPSEEK_API_KEY',
    'deepseek_base_url': 'DEEPSEEK_BASE_URL',
    'deepseek_model': 'DEEPSEEK_MODEL',
    'deepseek_daily_token_limit': 'DEEPSEEK_DAILY_TOKEN_LIMIT',
    'deepseek_monthly_token_limit': 'DEEPSEEK_MONTHLY_TOKEN_LIMIT',
}


def _get_setting(key, default=None):
    try:
        row = SystemSetting.query.filter_by(key=key).first()
        return row.value if (row and row.value is not None) else default
    except Exception:
        return default


def _set_setting(key, value, remark=None):
    row = SystemSetting.query.filter_by(key=key).first()
    uid = current_user.id if current_user.is_authenticated else None
    if row:
        row.value = value
        if remark is not None:
            row.remark = remark
        row.updated_by = uid
    else:
        db.session.add(SystemSetting(key=key, value=value, remark=remark, updated_by=uid))
    return row


def _apply_deepseek_settings():
    """启动时 / 超管保存后：把 DB 中 DeepSeek 配置应用到 Config 运行时值（DB 优先于环境变量）。"""
    applied = []
    v = _get_setting('deepseek_api_key')
    if v:
        Config.DEEPSEEK_API_KEY = v
        applied.append('api_key')
    v = _get_setting('deepseek_base_url')
    if v:
        Config.DEEPSEEK_BASE_URL = str(v).rstrip('/')
        applied.append('base_url')
    v = _get_setting('deepseek_model')
    if v:
        Config.DEEPSEEK_MODEL = v
        applied.append('model')
    for key, attr in (('deepseek_daily_token_limit', 'DEEPSEEK_DAILY_TOKEN_LIMIT'),
                      ('deepseek_monthly_token_limit', 'DEEPSEEK_MONTHLY_TOKEN_LIMIT')):
        v = _get_setting(key)
        if v is not None and str(v).strip() != '':
            try:
                setattr(Config, attr, max(0, int(float(v))))
                applied.append(key)
            except (TypeError, ValueError):
                pass
    return applied


def _mask_secret(s):
    """密钥掩码：sk-xxxxxxxxxxxxabcd -> sk-***********abcd"""
    if not s:
        return ''
    s = str(s)
    if len(s) <= 10:
        return '*' * len(s)
    return s[:3] + '*' * (len(s) - 7) + s[-4:]


# 反馈#18：BAGEL GPU 后端（autoDL 满血版 / 局域网优化版）运行时配置，持久化于 system_settings。
# 每个目标后端的可配置字段：(setting 键后缀, Config 属性, 类型)
_BAGEL_CONFIG_FIELDS = {
    'autodl': [
        ('enabled', 'BAGEL_AUTODL_ENABLED', 'bool'),
        ('service_url', 'BAGEL_AUTODL_SERVICE_URL', 'url'),
        ('service_token', 'BAGEL_AUTODL_SERVICE_TOKEN', 'str'),
        ('model', 'BAGEL_AUTODL_MODEL', 'str'),
        ('gpu_spec', 'BAGEL_AUTODL_GPU_SPEC', 'str'),
        ('remark', None, 'str'),
    ],
    'local': [
        ('enabled', 'BAGEL_LOCAL_ENABLED', 'bool'),
        ('os', 'BAGEL_LOCAL_OS', 'os'),
        ('service_url', 'BAGEL_LOCAL_SERVICE_URL', 'url'),
        ('service_token', 'BAGEL_LOCAL_SERVICE_TOKEN', 'str'),
        ('model_path', 'BAGEL_LOCAL_MODEL_PATH', 'str'),
        ('gpu_index', 'BAGEL_LOCAL_GPU_INDEX', 'str'),
        ('remark', None, 'str'),
    ],
}


def _bagel_setting_key(target, suffix):
    return f'bagel_{target}_{suffix}'


def _apply_bagel_settings():
    """启动时 / 管理员保存后：把 DB 中 BAGEL 后端配置应用到 Config 运行时值（DB 优先于环境变量）。"""
    applied = []
    for target, fields in _BAGEL_CONFIG_FIELDS.items():
        for suffix, attr, kind in fields:
            if not attr:
                continue
            v = _get_setting(_bagel_setting_key(target, suffix))
            if v is None or str(v).strip() == '':
                continue
            v = str(v).strip()
            try:
                if kind == 'bool':
                    setattr(Config, attr, v not in ('0', 'false', 'False', ''))
                elif kind == 'url':
                    setattr(Config, attr, v.rstrip('/'))
                elif kind == 'os':
                    setattr(Config, attr, v if v in ('windows', 'linux') else 'linux')
                else:
                    setattr(Config, attr, v)
                applied.append(_bagel_setting_key(target, suffix))
            except Exception:
                pass
    return applied


def _bagel_config_snapshot(target):
    """组装某 BAGEL 后端的当前配置（token 掩码返回）。"""
    fields = _BAGEL_CONFIG_FIELDS.get(target)
    if not fields:
        return None
    out = {'target': target}
    for suffix, attr, kind in fields:
        if attr:
            v = getattr(Config, attr, None)
        else:
            v = _get_setting(_bagel_setting_key(target, suffix), '') or ''
        if kind == 'bool':
            out[suffix] = bool(v)
        else:
            out[suffix] = '' if v is None else str(v)
    token = out.get('service_token') or ''
    out['service_token_masked'] = _mask_secret(token)
    out['service_token_set'] = bool(token)
    out.pop('service_token', None)
    return out


def _log_login_event(user_id, username_attempted, success, event_type, message=None):
    """登录/登出/限流事件统一入 system_event_logs（event_type='login'，action_type 为具体子类）。"""
    try:
        detail = {
            'username_attempted': username_attempted or '',
            'user_agent': (request.user_agent.string or '')[:500] if request.user_agent else '',
        }
        row = SystemEventLog(
            event_type='login',
            actor_id=user_id,
            success=bool(success),
            ip_address=_client_ip(),
            action_type=event_type,
            summary=(message or username_attempted or event_type or '')[:255],
            detail_json=json.dumps(detail, ensure_ascii=False),
        )
        db.session.add(row)
        db.session.commit()
    except Exception:
        db.session.rollback()


def _log_user_audit(actor_id, target_user_id, action_type, detail=None):
    """账号权限/密码/资料类审计统一入 system_event_logs（event_type='user_audit'）。
    仅 add 不 commit，由调用方随主事务一起提交（写库失败不阻断主流程）。"""
    try:
        row = SystemEventLog(
            event_type='user_audit',
            actor_id=actor_id,
            target_user_id=target_user_id,
            success=True,
            action_type=action_type,
            summary=action_type,
            detail_json=json.dumps(detail, ensure_ascii=False) if detail else None,
        )
        db.session.add(row)
    except Exception:
        db.session.rollback()


def _login_rate_check():
    """登录接口固定窗口限流（按 IP，Redis INCR + EXPIRE）。
    返回 (limited, wait_seconds, key)；Redis 异常时降级放行。"""
    try:
        max_n = int(app.config.get('LOGIN_RATE_LIMIT_MAX', 20) or 20)
        window = int(app.config.get('LOGIN_RATE_WINDOW_SECONDS', 300) or 300)
        if max_n <= 0:
            return False, 0, None
        ip = _client_ip()
        key = 'security:login_rl:%s' % ip
        n, ttl = _login_rl_script(keys=[key], args=[window])
        n, ttl = int(n), int(ttl)
        if n > max_n:
            return True, (ttl if ttl and ttl > 0 else window), key
        return False, 0, key
    except Exception as e:
        app.logger.warning('login rate limit check failed: %s', e)
        return False, 0, None


def _safe_emit_account(recipient_ids, msg_type, detail, **kwargs):
    """账号类站内信：失败不影响主事务。"""
    try:
        from services.messaging import emit_account_notification

        emit_account_notification(recipient_ids, msg_type, detail, **kwargs)
        db.session.commit()
    except Exception:
        db.session.rollback()


_MSG_PRIO = {'urgent': 4, 'high': 3, 'medium': 2, 'low': 1}


def _serialize_inbox_row(m):
    return {
        'msg_key': f'inbox:{m.id}',
        'source': 'inbox',
        'id': m.id,
        'category': m.category,
        'msg_type': m.msg_type,
        'priority': m.priority,
        'title': m.title,
        'summary': m.summary,
        'body': m.body,
        'business_type': m.business_type,
        'business_id': m.business_id,
        'action_url': m.action_url,
        'read_at': m.read_at.isoformat() if m.read_at else None,
        'pinned_at': m.pinned_at.isoformat() if m.pinned_at else None,
        'created_at': m.created_at.isoformat() if m.created_at else None,
        'sender_type': m.sender_type,
    }


@login_manager.user_loader
def load_user(user_id):
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    u = User.query.get(uid)
    if not u or not u.is_active or u.is_locked_account:
        return None
    if u.account_valid_until and u.account_valid_until < datetime.utcnow():
        return None
    return u


@app.before_request
def _session_and_password_gate():
    ep = request.endpoint
    if ep in (None, 'login', 'logout', 'static', 'switch_language'):
        return
    if request.path.startswith('/static/'):
        return
    if not current_user.is_authenticated:
        return
    if getattr(current_user, 'must_change_password', False):
        if ep != 'change_initial_password':
            return redirect(url_for('change_initial_password'))
    idle_sec = app.config.get('SESSION_IDLE_TIMEOUT', 1800)
    now = time.time()
    if session.get('_sv', -1) != (current_user.session_version or 0):
        logout_user()
        session.clear()
        flash('会话已失效（可能被管理员强制下线），请重新登录', 'warning')
        return redirect(url_for('login'))
    last = session.get('_idle_ts')
    if last and (now - last) > idle_sec:
        _log_login_event(current_user.id, current_user.username, False, 'idle_timeout', '无操作超时')
        logout_user()
        session.clear()
        flash('长时间未操作，已自动退出', 'info')
        return redirect(url_for('login'))
    session['_idle_ts'] = now


# Language switching route
@app.route('/switch_language/<language>')
def switch_language(language):
    if language in ['en', 'cn']:
        session['language'] = language
    return redirect(request.referrer or url_for('index'))

# Helper function to get current language
def get_language():
    return session.get('language', 'cn')  # Default to Chinese


def allowed_file(filename):
    ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
    return ext in app.config['ALLOWED_EXTENSIONS']


def _unique_storage_name(original_filename):
    """f3：上传文件统一以 uuid 落盘、仅保留校验过的扩展名——
    避免同名文件互相覆盖、中文文件名被 secure_filename 清空、并发上传互相污染。
    返回 (storage_name, ext)；类型不允许返回 (None, None)。"""
    ext = original_filename.rsplit('.', 1)[1].lower() if '.' in original_filename else ''
    if ext not in app.config['ALLOWED_EXTENSIONS']:
        return None, None
    return f'{uuid.uuid4().hex}.{ext}', ext


def detect_file_type(filename):
    ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
    if ext in {'mp4', 'avi', 'mov', 'mkv', 'webm', 'flv'}:
        return 'video'
    if ext in {'mp3', 'wav', 'flac', 'm4a'}:
        return 'audio'
    if ext in {'jpg', 'jpeg', 'png', 'webp', 'gif'}:
        return 'image'
    return 'unknown'


def detect_modality(filename):
    ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
    if ext in {'mp3', 'wav', 'flac', 'm4a'}:
        return 'audio'
    if ext in {'mp4', 'avi', 'mov', 'mkv', 'webm', 'flv'}:
        return 'video'
    # 反馈#8：图片模态（BAGEL 图像理解）——jpg/jpeg/png/webp/gif
    if ext in {'jpg', 'jpeg', 'png', 'webp', 'gif'}:
        return 'image'
    return 'unknown'


def compute_file_md5(file_path, chunk_size=4 * 1024 * 1024):
    md5 = hashlib.md5()
    with open(file_path, 'rb') as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            md5.update(chunk)
    return md5.hexdigest()


def generate_task_no(prefix='TASK'):
    # 反馈#13：编号后缀改为 8 位纯数字（时间戳 14 位 + 随机 8 位，
    # 编号中至少含 8 位连续数字，位数更长、肉眼可读，唯一性由查重兜底）
    date_part = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    rand_part = f'{random.randint(0, 99_999_999):08d}'
    return f"{prefix}-{date_part}-{rand_part}"


# 反馈#12 部署时自动补位的占位昵称（「用户<ID>」），反馈#13 起改为用户名派生名
_NICK_PLACEHOLDER_RE = re.compile(r'^用户\d+$')


def _derive_nickname_from_username(username):
    """反馈#13：默认昵称由用户名派生（如 zhou.yating -> yating、yating05 -> yating05），
    取最后一个点/横杠/下划线/@/空格分隔段，保留字母数字与中文；取不到则回退整段清洗。"""
    raw = (username or '').strip()
    parts = re.split(r'[._@\-\s]+', raw)
    cand = parts[-1] if parts and parts[-1] else raw
    cand = re.sub(r'[^0-9A-Za-z一-鿿]', '', cand)
    if not cand:
        cand = re.sub(r'[^0-9A-Za-z一-鿿]', '', raw) or 'user'
    return cand[:80]


def _unique_derived_nickname(username, extra_used=None):
    """基于用户名生成唯一昵称：重名时追加两位序号（yating -> yating02、yating03…）。"""
    base = _derive_nickname_from_username(username)
    used = set(extra_used or [])
    try:
        used |= {n for (n,) in db.session.query(User.nickname).filter(User.nickname.isnot(None)).all()}
    except Exception:
        pass
    if base not in used:
        return base
    for i in range(2, 100):
        cand = f'{base}{i:02d}'
        if cand not in used:
            return cand
    return f'{base}{uuid.uuid4().hex[:4]}'


def fill_acquisition_fields(recording, uploader_id, source_channel='upload'):
    """表精简后：采集元数据直接写 recordings 列（原 acquisition_metadata 表已并入）。"""
    file_size = 0
    file_md5 = ''
    if recording.file_path and os.path.exists(recording.file_path):
        file_size = os.path.getsize(recording.file_path)
        file_md5 = compute_file_md5(recording.file_path)
    recording.uploader_id = uploader_id
    recording.file_type = detect_file_type(recording.filename)
    recording.file_size = file_size or None
    recording.file_md5 = file_md5 or None
    recording.source_channel = source_channel
    recording.acquisition_task_no = generate_task_no('COLLECT')
    recording.acquisition_audit_status = 'pending'
    # 修复反馈#2：新采集数据必须进入任务池——task_type=2 为转录任务池（数据处理页
    # /api/process/next-task?type=2 按 status='pending' AND task_type=2 领取）；
    # 不设置时 task_type 为 NULL，转录池/质检池（task_type=1）均无法匹配，表现为"没有更多可用任务"。
    if recording.task_type is None:
        recording.task_type = 2
    return recording


def _is_reviewer():
    return current_user.is_authenticated and current_user.role in ('inspector', 'admin', 'super_admin')


# 反馈#7 权限矩阵：数据采集与处理人员（recorder）及管理员可采集/手动处理/算法调用；
# 质检审核人员（inspector）无数据采集、数据处理权限（其作业在审核模块 /audit）。
def _is_processor():
    return current_user.is_authenticated and current_user.role in ('recorder', 'admin', 'super_admin')


# f2 安全修复 + 反馈#7 权限矩阵：全量数据管理（/api/manage/* 数据访问类）仅系统管理员/超级管理员。
# 注：无效数据「标记无效」(invalidate) 按需求文档另行放行 inspector（见 _invalidate_recording_core）。
_MANAGE_DATA_ROLES = ('admin', 'super_admin')


def _can_access_manage_data():
    return current_user.is_authenticated and current_user.role in _MANAGE_DATA_ROLES


def _guard_super_admin_target(target_user):
    """f2：对 super_admin 账号的管理操作仅 super_admin 本人/同角色可执行，防止管理员越权处置超管。

    返回 None 表示放行；返回 Flask 响应表示拒绝。
    """
    if target_user is not None and target_user.role == 'super_admin' and (
        not current_user.is_authenticated or current_user.role != 'super_admin'
    ):
        return jsonify({'error': '无权操作超级管理员账号'}), 403
    return None


def _crawl_videos_dir():
    """爬虫下载目录白名单根目录（<cwd>/videos）。"""
    return os.path.abspath(os.path.join(os.getcwd(), 'videos'))


def _is_path_within(base_dir, target_path):
    base = os.path.abspath(base_dir)
    target = os.path.abspath(target_path)
    try:
        return os.path.commonpath([base, target]) == base
    except ValueError:
        return False


# ---- f3：人工处理任务领取锁（Redis 短期锁，防止多人重复领取/保存同一条录音）----
_PROCESS_CLAIM_TTL = 900  # 15 分钟，重复领取/保存自动续期
_PROCESS_CLAIM_KEY_PREFIX = 'security:process_claim:'


def _claim_owner(recording_id):
    """返回持锁用户 id（int）；无锁返回 None。Redis 异常降级为 None（不阻塞业务）。"""
    try:
        v = model_redis.get(f'{_PROCESS_CLAIM_KEY_PREFIX}{recording_id}')
        return int(v) if v not in (None, '') else None
    except Exception:
        app.logger.warning('读取处理领取锁失败 recording=%s', recording_id, exc_info=True)
        return None


def _acquire_claim(recording_id, user_id):
    """为 user_id 领取录音：SET NX；自己已持锁则续期；他人持锁返回 False。
    Redis 异常时降级放行（与登录限流降级策略一致）。"""
    key = f'{_PROCESS_CLAIM_KEY_PREFIX}{recording_id}'
    try:
        if model_redis.set(key, str(user_id), nx=True, ex=_PROCESS_CLAIM_TTL):
            return True
        owner = model_redis.get(key)
        if owner is not None and str(owner) == str(user_id):
            model_redis.expire(key, _PROCESS_CLAIM_TTL)
            return True
        return False
    except Exception:
        app.logger.warning('领取锁写入失败 recording=%s，降级放行', recording_id, exc_info=True)
        return True


def _release_claim(recording_id, user_id):
    """任务离开待处理池（提交/打回完成）后释放锁；仅持锁人可释放。"""
    key = f'{_PROCESS_CLAIM_KEY_PREFIX}{recording_id}'
    try:
        owner = model_redis.get(key)
        if owner is not None and str(owner) == str(user_id):
            model_redis.delete(key)
    except Exception:
        app.logger.warning('释放领取锁失败 recording=%s', recording_id, exc_info=True)


def _fixed_window_rate_check(key, max_n, window):
    """通用固定窗口限流（复用登录限流 Lua 脚本）。
    返回 (limited, wait_seconds)；Redis 异常降级放行。"""
    try:
        n, ttl = _login_rl_script(keys=[key], args=[window])
        n, ttl = int(n), int(ttl)
        if n > max_n:
            return True, (ttl if ttl and ttl > 0 else window)
        return False, 0
    except Exception as e:
        app.logger.warning('rate limit check failed for %s: %s', key, e)
        return False, 0


def _latest_processing_result(recording_id):
    return (
        ProcessingResult.query.filter_by(recording_id=recording_id)
        .order_by(ProcessingResult.processed_at.desc())
        .first()
    )


def _sync_acquisition_audit(recording, audit_status):
    """表精简后：采集审核状态直接写 recordings.acquisition_audit_status。"""
    recording.acquisition_audit_status = audit_status


def _append_audit_log(recording_id, action_type, audit_result, comments='', detail=None):
    row = AuditLog(
        recording_id=recording_id,
        auditor_id=current_user.id,
        action_type=action_type,
        audit_result=audit_result,
        comments=comments or '',
        detail_json=json.dumps(detail, ensure_ascii=False) if detail else None,
    )
    db.session.add(row)


def _send_audit_reject_message(recording, processor_id, comments):
    if not processor_id:
        return
    from services.messaging import emit_audit_inbox

    # 表精简后：审核打回站内信统一走 inbox_messages（category='audit'），不再写 audit_messages
    emit_audit_inbox(
        [processor_id],
        current_user.id,
        recording.id,
        recording.filename or '',
        comments or '',
    )


def _build_audit_preview(recording):
    """审核页：来源、处理人、元数据、多模态预览字段、合规性标记。"""
    pr = _latest_processing_result(recording.id)
    processor = User.query.get(pr.processor_id) if pr else None
    uploader = User.query.get(recording.uploader_id) if recording.uploader_id else None
    timeline = []
    if pr and pr.timeline_json:
        try:
            timeline = json.loads(pr.timeline_json)
        except (json.JSONDecodeError, TypeError):
            timeline = []
    file_ok = bool(recording.file_path and os.path.isfile(recording.file_path))
    meta_ok = bool(
        recording.file_md5
        and recording.acquisition_task_no
        and recording.uploader_id
        and recording.created_at
    )
    modality = detect_modality(recording.filename)
    media_url = f'/uploads/{recording.filename}' if file_ok else None
    return {
        'recording_id': recording.id,
        'filename': recording.filename,
        'status': recording.status,
        'modality': modality,
        'duration': recording.duration,
        'media_url': media_url,
        'text_content': recording.text_content or '',
        'subtitle_content': recording.subtitle_content,
        'timeline': timeline,
        'compliance': {
            'file_readable': file_ok,
            'metadata_complete': meta_ok,
            'md5': recording.file_md5,
            'task_no': recording.acquisition_task_no,
            'source_channel': recording.source_channel,
        },
        'processor': {'id': processor.id, 'username': processor.username} if processor else None,
        'uploader': {'id': uploader.id, 'username': uploader.username} if uploader else None,
        'uploaded_at': recording.created_at.isoformat() if recording.created_at else None,
        'last_processed_at': pr.processed_at.isoformat() if pr and pr.processed_at else None,
        'process_mode': pr.process_mode if pr else None,
        'review_remark': recording.review_remark or '',
    }


def _mark_review_seen(recording):
    """审核员打开待审任务即留痕（review_seen）——提交人端据此显示'审核员已查看'，
    且已查看的任务提交人不能撤回。已存在则不重复记录。"""
    try:
        exists = (
            AuditLog.query.filter_by(recording_id=recording.id, action_type='review_seen').first()
        )
        if not exists:
            _append_audit_log(recording.id, 'review_seen', 'seen', comments='审核员已打开查看')
            db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.warning('review_seen 留痕失败 recording=%s', recording.id, exc_info=True)


def load_bilibili_crawler_class():
    crawler_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scripts', 'bilibili_crawler.py')
    spec = importlib.util.spec_from_file_location("bilibili_crawler_module", crawler_path)
    if spec is None or spec.loader is None:
        raise RuntimeError('Cannot load bilibili crawler module')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.BilibiliCrawler

# Routes for the five main modules
@app.route('/')
@login_required
def index():
    lang = get_language()
    return render_template(f'index_{lang}.html')

# 1. Data Upload Module
@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload():
    # 反馈#7 权限矩阵：数据采集仅采集与处理人员/管理员；质检审核人员无采集权限
    if not _is_processor():
        if request.method == 'POST':
            return jsonify({'error': '仅数据采集与处理人员可上传/采集数据'}), 403
        flash('数据采集模块面向采集与处理人员，审核人员请在「数据审核」模块作业。', 'danger')
        return redirect(url_for('audit_page'))
    if request.method == 'POST':
        # Support single or multiple files from field name 'file'
        files = request.files.getlist('file')
        if not files or all((not f or not f.filename) for f in files):
            return jsonify({'error': 'No selected file'}), 400

        created_ids = []
        for file in files:
            if not file or not file.filename:
                continue
            if not allowed_file(file.filename):
                return jsonify({'error': f'Unsupported file type: {file.filename}'}), 400

            # f3：uuid 落盘，避免同名覆盖与中文文件名被清空
            storage_name, _ext = _unique_storage_name(file.filename)
            if not storage_name:
                return jsonify({'error': f'Unsupported file type: {file.filename}'}), 400
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], storage_name)
            file.save(filepath)

            recording = Recording(
                filename=storage_name,
                file_path=filepath,
                recorded_by=current_user.id,
                status='pending'
            )
            db.session.add(recording)
            db.session.flush()  # get recording.id
            fill_acquisition_fields(recording, current_user.id, source_channel='upload')
            created_ids.append(recording.id)

        db.session.commit()
        return jsonify({'success': True, 'recording_ids': created_ids, 'count': len(created_ids)})
    
    lang = get_language()
    return render_template(f'upload_{lang}.html')

# 2. Data Processing Module
@app.route('/process')
@login_required
def process():
    # 反馈#7 权限矩阵：数据处理（转写/算法）面向采集与处理人员；审核人员在 /audit 作业
    if not _is_processor():
        flash('数据处理模块面向采集与处理人员，审核人员请在「数据审核」模块作业。', 'danger')
        return redirect(url_for('audit_page'))
    recordings = Recording.query.filter_by(status='pending').all()
    lang = get_language()
    return render_template(f'process_{lang}.html', recordings=recordings)

# API Endpoints for processing module
@app.route('/api/process/next-task')
@login_required
def get_next_task():
    task_type = request.args.get('type', type=int)

    # f2 安全：类型1为质检/检查任务，仅审核员（inspector/admin/super_admin）可领取
    if task_type == 1 and not _is_reviewer():
        return jsonify({'error': '仅审核人员可领取检查任务'}), 403

    # 反馈#7：类型2转录/处理任务仅采集与处理人员（recorder/admin/super_admin）可领取
    if task_type != 1 and not _is_processor():
        return jsonify({'error': '仅数据采集与处理人员可领取转录处理任务'}), 403

    # f3：按任务类型取候选池，跳过他人持锁的录音，并为当前用户原子加领取锁
    q = Recording.query.filter_by(status='pending').filter(Recording.deleted_at.is_(None))
    if task_type == 1:
        q = q.filter_by(task_type=1)
    else:
        q = q.filter_by(task_type=2)

    for recording in q.order_by(Recording.id.asc()).all():
        owner = _claim_owner(recording.id)
        if owner is not None and owner != current_user.id:
            continue  # 他人正在处理，跳过
        if not _acquire_claim(recording.id, current_user.id):
            continue  # 并发竞争落败，尝试下一条
        video_url = f'/uploads/{recording.filename}'
        task_data = {
            'id': recording.id,
            'video_url': video_url,
            'text': recording.text_content or '',
            # 反馈#8：回传模态（image/audio/video）供前端渲染图片/媒体；类型1检查
            # 同样回带字幕时间轴（抓取了字幕的视频在检查面板可按时间轴校对）
            'modality': detect_modality(recording.filename),
            'filename': recording.filename,
        }
        # 草稿/撤回后重新编辑：回带已保存的时间轴，处理人刷新页面不丢失进度
        tl = []
        pr = _latest_processing_result(recording.id)
        if pr and pr.timeline_json:
            try:
                tl = json.loads(pr.timeline_json)
            except (json.JSONDecodeError, TypeError):
                tl = []
        task_data['timeline'] = tl
        return jsonify({'task': task_data})
    return jsonify({'task': None})

@app.route('/api/process/next-task-unified')
@login_required
def get_next_task_unified():
    """反馈#14：手动处理合并为单一队列——按权限把待检查（task_type=1，仅审核员）
    与待转录/描述（task_type=2，采集处理人员）候选合流，按 ID 升序领取最早一条，
    返回体带 kind（1=检查 / 2=转录处理）与采集人信息，前端单面板按 kind 渲染。"""
    can_review = _is_reviewer()
    can_process = _is_processor()
    if not (can_review or can_process):
        return jsonify({'error': '无处理权限'}), 403

    # 反馈#14：「跳过/下一条」——前端把本次会话已跳过的任务 id 以 ?skip=1,2,3 传回，
    # 后端释放这些任务上本人的领取锁（退回池中供他人领取），并在本轮候选中排除。
    skip_ids = set()
    for part in (request.args.get('skip') or '').split(','):
        part = part.strip()
        if part.isdigit():
            skip_ids.add(int(part))
    for sid in skip_ids:
        _release_claim(sid, current_user.id)

    candidates = []
    if can_review:
        candidates += Recording.query.filter_by(status='pending', task_type=1) \
            .filter(Recording.deleted_at.is_(None)).all()
    if can_process:
        candidates += Recording.query.filter_by(status='pending', task_type=2) \
            .filter(Recording.deleted_at.is_(None)).all()
    if skip_ids:
        candidates = [r for r in candidates if r.id not in skip_ids]
    candidates.sort(key=lambda r: r.id)

    for recording in candidates:
        owner = _claim_owner(recording.id)
        if owner is not None and owner != current_user.id:
            continue
        if not _acquire_claim(recording.id, current_user.id):
            continue
        uploader = User.query.get(recording.uploader_id) if recording.uploader_id else None
        task_data = {
            'id': recording.id,
            'video_url': f'/uploads/{recording.filename}',
            'text': recording.text_content or '',
            'modality': detect_modality(recording.filename),
            'filename': recording.filename,
            'kind': recording.task_type if recording.task_type in (1, 2) else 2,
            'task_no': recording.acquisition_task_no or '',
            'source_channel': recording.source_channel or '',
            'uploader_id': recording.uploader_id,
            'uploader_name': (getattr(uploader, 'nickname', None) or (uploader.username if uploader else '')) or '',
            'is_own': bool(recording.uploader_id == current_user.id),
        }
        tl = []
        pr = _latest_processing_result(recording.id)
        if pr and pr.timeline_json:
            try:
                tl = json.loads(pr.timeline_json)
            except (json.JSONDecodeError, TypeError):
                tl = []
        task_data['timeline'] = tl
        return jsonify({'task': task_data})
    return jsonify({'task': None})


@app.route('/api/process/algo-candidates')
@login_required
def process_algo_candidates():
    """反馈#14：算法处理"录制数据 ID"下拉候选——当前用户有权限处理的记录
    （待处理/待修正池），分"本人采集 / 他人采集"两组返回。"""
    if not _is_processor():
        return jsonify({'error': '仅数据采集与处理人员可调用算法处理'}), 403
    rows = (Recording.query
            .filter(Recording.deleted_at.is_(None),
                    Recording.status.in_(['pending', 'pending_fix']),
                    Recording.task_type == 2)
            .order_by(Recording.id.asc()).all())
    # 反馈#14：英文界面返回英文标签（?lang=en），默认中文
    if request.args.get('lang', '') == 'en':
        mod_label = {'image': 'Image', 'video': 'Video', 'audio': 'Audio'}
        src_label = {'crawl': 'Web crawl', 'record': 'Field recording', 'upload': 'Local upload'}
        mod_key, src_key = 'modality_label_en', 'source_label_en'
    else:
        mod_label = {'image': '图片', 'video': '视频', 'audio': '音频'}
        src_label = {'crawl': '互联网采集', 'record': '现场录制', 'upload': '本地上传'}
        mod_key, src_key = 'modality_label', 'source_label'
    own, others = [], []
    for r in rows:
        if r.status == 'pending_fix':
            pr = _latest_processing_result(r.id)
            if not (_is_system_admin() or (pr and pr.processor_id == current_user.id)):
                continue
        up = User.query.get(r.uploader_id) if r.uploader_id else None
        item = {
            'id': r.id,
            'filename': r.filename,
            'modality': detect_modality(r.filename),
            mod_key: mod_label.get(detect_modality(r.filename), ('Other' if mod_key.endswith('_en') else '其他')),
            src_key: src_label.get(r.source_channel or '', r.source_channel or ('Other' if mod_key.endswith('_en') else '其他')),
            'status': r.status,
            'uploader_id': r.uploader_id,
            'uploader_name': (getattr(up, 'nickname', None) or (up.username if up else '')) or ('—' if mod_key == 'modality_label' else '-'),
        }
        (own if r.uploader_id == current_user.id else others).append(item)
    return jsonify({'own': own, 'others': others})


@app.route('/api/process/inspect', methods=['POST'])
@login_required
def process_inspection():
    """兼容旧版「类型1检查」；优先按 recording_id 定位，否则取首条 pending+task_type=1。"""
    # f2 安全：检查（通过/打回）是审核员动作，普通采集/转录账号无权自审
    if not _is_reviewer():
        return jsonify({'error': '仅审核人员可执行检查'}), 403
    data = request.get_json(silent=True) or {}
    recording_id = data.get('recording_id')
    action = data.get('action')  # 'approve' or 'reject'
    corrections = data.get('corrections', '')
    comments = data.get('comments', '')
    # 反馈#8：类型1检查支持字幕时间轴校对（与转录页同一套 hh:mm:ss 编辑器）
    timeline = data.get('timeline')
    if timeline is not None and not isinstance(timeline, list):
        timeline = None

    if recording_id:
        recording = Recording.query.get(recording_id)
        if not recording or recording.status != 'pending' or recording.task_type != 1:
            return jsonify({'error': '录音不存在或不是待检查状态'}), 404
    else:
        # f3：无 recording_id 时仅在当前用户已领取的检查任务中取，避免静默抢到他人任务
        recording = None
        for r in Recording.query.filter_by(status='pending', task_type=1).filter(Recording.deleted_at.is_(None)).order_by(Recording.id.asc()).all():
            if _claim_owner(r.id) == current_user.id:
                recording = r
                break
        if not recording:
            return jsonify({'error': '没有找到您已领取的待检查录音，请先领取任务'}), 409

    # f3：领取锁校验——他人正在处理的任务不可检查
    owner = _claim_owner(recording.id)
    if owner is not None and owner != current_user.id:
        return jsonify({'error': '该任务正由其他人员处理中'}), 409
    _acquire_claim(recording.id, current_user.id)

    # 反馈#8：检查面板校对后的字幕时间轴随检查结果持久化（写入 ProcessingResult，
    # 与转录页同一数据结构），后续查看/审核均以校对后的时间轴为准
    if timeline:
        pr = _latest_processing_result(recording.id)
        if pr is None:
            pr = ProcessingResult(
                recording_id=recording.id,
                processor_id=current_user.id,
                modality=detect_modality(recording.filename),
                process_mode='manual',
                processed_at=datetime.utcnow(),
            )
            db.session.add(pr)
        pr.timeline_json = json.dumps(timeline, ensure_ascii=False)
        seg_texts = [str(s.get('text') or '').strip()
                     for s in timeline if isinstance(s, dict)]
        if corrections:
            pr.content_text = corrections
        elif any(seg_texts):
            pr.content_text = '\n'.join(t for t in seg_texts if t)
            if not recording.text_content:
                recording.text_content = pr.content_text
        try:
            recording.subtitle_json_data = json.dumps(timeline, ensure_ascii=False)
        except Exception:
            pass

    # 表精简：原 inspections 表并入 audit_logs（action_type='inspection'，corrections 入 detail_json）
    _append_audit_log(
        recording.id,
        'inspection',
        'approved' if action == 'approve' else 'rejected',
        comments=comments or corrections,
        detail={'legacy_action': action, 'corrections': corrections or '',
                'timeline_segments': len(timeline) if timeline else 0},
    )
    recording.status = 'completed' if action == 'approve' else 'rejected'
    if corrections:
        recording.text_content = corrections
    if action == 'approve':
        _sync_acquisition_audit(recording, 'pass')
    db.session.commit()
    _release_claim(recording.id, current_user.id)  # 任务离池，释放领取锁
    return jsonify({'success': True, 'message': '检查提交成功'})

@app.route('/api/process/transcribe', methods=['POST'])
@login_required
def process_transcription():
    # 反馈#7：转录/手动处理为采集与处理人员动作，审核人员无权
    if not _is_processor():
        return jsonify({'error': '仅数据采集与处理人员可执行转录处理'}), 403
    data = request.get_json(silent=True) or {}
    transcription = data.get('transcription', '') or ''
    action = (data.get('action') or 'save').strip()  # save=草稿保存（可继续修改）/ submit=提交审核
    if action not in ('save', 'submit'):
        return jsonify({'error': 'action 非法'}), 400

    # f3：必须定位到明确的录音——优先用 body.recording_id；
    # 缺省时仅在当前用户已领取（next-task 加锁）的待转录任务中取，
    # 不再静默「取首条 pending」导致多人互相覆盖转录结果。
    recording_id = data.get('recording_id')
    try:
        recording_id = int(recording_id) if recording_id is not None else None
    except (TypeError, ValueError):
        return jsonify({'error': 'recording_id 非法'}), 400

    editable_status = ('pending', 'pending_fix')
    if recording_id:
        recording = Recording.query.get(recording_id)
        if not recording or recording.task_type != 2 or recording.status not in editable_status:
            return jsonify({'error': '录音不存在或当前状态不可编辑'}), 404
    else:
        recording = None
        for r in (Recording.query
                  .filter(Recording.status.in_(editable_status), Recording.task_type == 2)
                  .order_by(Recording.id.asc()).all()):
            if _claim_owner(r.id) == current_user.id:
                recording = r
                break
        if not recording:
            return jsonify({'error': '请先领取转录任务后再保存（或在请求中提供 recording_id）'}), 409

    # f3：领取锁校验——他人正在转录的任务不可写
    owner = _claim_owner(recording.id)
    if owner is not None and owner != current_user.id:
        return jsonify({'error': '该任务正由其他人员转录中'}), 409
    _acquire_claim(recording.id, current_user.id)

    timeline = data.get('timeline', []) or []
    recording.text_content = transcription
    if timeline:
        recording.subtitle_json_data = json.dumps(timeline, ensure_ascii=False)

    # 草稿 upsert：同一处理人多次保存复用同一结果行，避免结果表无限增长
    result_row = (
        ProcessingResult.query.filter_by(
            recording_id=recording.id, processor_id=current_user.id, process_mode='manual')
        .order_by(ProcessingResult.processed_at.desc())
        .first()
    )
    if result_row is None:
        result_row = ProcessingResult(
            recording_id=recording.id,
            processor_id=current_user.id,
            modality=detect_modality(recording.filename),
            process_mode='manual',
        )
        db.session.add(result_row)
    result_row.content_text = transcription
    result_row.timeline_json = json.dumps(timeline, ensure_ascii=False) if timeline else '[]'
    result_row.processed_at = datetime.utcnow()

    if action == 'submit':
        recording.status = 'pending_review'
        _sync_acquisition_audit(recording, 'pending')
        _append_audit_log(
            recording.id, 'transcribe_submit', 'submitted',
            comments=transcription[:200],
            detail={'timeline_segments': len(timeline)},
        )
        db.session.commit()
        _release_claim(recording.id, current_user.id)  # 进入审核池，释放转录领取锁
        return jsonify({'success': True, 'submitted': True, 'message': '转录已提交，等待审核'})

    # action == 'save'：草稿保存——任务留在待处理池、领取锁续期，处理人可继续修改
    db.session.commit()
    return jsonify({'success': True, 'saved': True, 'draft': True, 'message': '草稿已保存'})


def _has_audit_action(recording_id, action_type):
    return (
        AuditLog.query.filter_by(recording_id=recording_id, action_type=action_type).first()
        is not None
    )


@app.route('/api/process/withdraw', methods=['POST'])
@login_required
def process_withdraw():
    """转录提交后、审核员尚未查看时，原提交人可撤回继续修改。"""
    data = request.get_json(silent=True) or {}
    recording_id = data.get('recording_id')
    try:
        recording_id = int(recording_id) if recording_id is not None else None
    except (TypeError, ValueError):
        return jsonify({'error': 'recording_id 非法'}), 400
    recording = Recording.query.get(recording_id) if recording_id else None
    if not recording or recording.task_type != 2:
        return jsonify({'error': '录音不存在'}), 404
    if recording.status != 'pending_review':
        return jsonify({'error': '仅待审核状态的任务可以撤回'}), 400

    pr = _latest_processing_result(recording.id)
    is_owner = bool(pr and pr.processor_id == current_user.id)
    if not (is_owner or _is_system_admin()):
        return jsonify({'error': '仅提交人（或管理员）可以撤回'}), 403

    if _has_audit_action(recording.id, 'review_seen') and not _is_system_admin():
        return jsonify({'error': '审核员已查看/开始处理，无法撤回'}), 409

    recording.status = 'pending'
    _append_audit_log(recording.id, 'transcribe_withdraw', 'withdrawn',
                      comments='提交人撤回修改')
    db.session.commit()
    _acquire_claim(recording.id, current_user.id)  # 撤回后立即由本人持锁继续编辑

    timeline = []
    if pr and pr.timeline_json:
        try:
            timeline = json.loads(pr.timeline_json)
        except (json.JSONDecodeError, TypeError):
            timeline = []
    return jsonify({'success': True, 'message': '已撤回，可继续修改',
                    'text': recording.text_content or '', 'timeline': timeline})


@app.route('/api/process/submission-status/<int:recording_id>')
@login_required
def process_submission_status(recording_id):
    """处理人查看本人提交任务的审核进度（等待审核 / 审核员已查看 / 审核结果）。"""
    recording = Recording.query.get(recording_id)
    if not recording or recording.task_type != 2:
        return jsonify({'error': 'not found'}), 404
    pr = _latest_processing_result(recording.id)
    is_owner = bool(pr and pr.processor_id == current_user.id)
    if not (is_owner or _is_reviewer()):
        return jsonify({'error': 'Forbidden'}), 403

    seen = _has_audit_action(recording.id, 'review_seen')
    status = recording.status
    if status in ('pending', 'pending_fix'):
        state = 'editing'
    elif status == 'pending_review':
        state = 'seen' if seen else 'waiting'
    elif status == 'approved':
        state = 'approved'
    elif status == 'rejected':
        state = 'rejected'
    else:
        state = status
    can_withdraw = (
        status == 'pending_review' and not seen
        and (is_owner or _is_system_admin())
    )

    seen_log = (
        AuditLog.query.filter_by(recording_id=recording.id, action_type='review_seen')
        .order_by(AuditLog.created_at.asc()).first()
    )
    auditor = User.query.get(seen_log.auditor_id) if seen_log else None
    timeline = []
    if pr and pr.timeline_json:
        try:
            timeline = json.loads(pr.timeline_json)
        except (json.JSONDecodeError, TypeError):
            timeline = []
    return jsonify({
        'recording_id': recording.id,
        'status': status,
        'state': state,
        'review_seen': seen,
        'can_withdraw': can_withdraw,
        'auditor': auditor.username if auditor else None,
        'seen_at': seen_log.created_at.isoformat() if seen_log and seen_log.created_at else None,
        'text': recording.text_content or '',
        'timeline': timeline,
    })


@app.route('/api/process/next-task-v2')
@login_required
def get_next_task_v2():
    """
    Role + modality based task allocation for data processing module.
    query:
      - modality: audio|video (optional)
      - mode: manual|algorithm (optional, reserved)
    """
    # 反馈#7：v2 分配用于转录/处理，仅采集与处理人员可领取
    if not _is_processor():
        return jsonify({'error': '仅数据采集与处理人员可领取处理任务'}), 403
    modality = request.args.get('modality', '').strip().lower()

    pending_rows = Recording.query.filter(Recording.status == 'pending').order_by(Recording.created_at.asc()).all()
    fix_rows = Recording.query.filter(Recording.status == 'pending_fix').order_by(Recording.created_at.asc()).all()
    # 打回修正：仅原处理人（或管理员）可再次领取
    fix_for_user = []
    for r in fix_rows:
        pr = _latest_processing_result(r.id)
        if not pr:
            continue
        if _is_system_admin() or pr.processor_id == current_user.id:
            fix_for_user.append(r)

    candidates = list(pending_rows) + fix_for_user

    def _match_role_and_modality(r):
        r_mod = detect_modality(r.filename)
        # 反馈#8：modality 预留过滤参数（audio/video/image），前端暂不传，行为不变
        if modality in {'audio', 'video', 'image'} and r_mod != modality:
            return False
        return True

    matched = [r for r in candidates if _match_role_and_modality(r)]
    if not matched:
        return jsonify({'task': None})

    # f3：跳过他人持锁任务并原子领取，避免多人同时打开同一条数据
    recording = None
    for r in matched:
        owner = _claim_owner(r.id)
        if owner is not None and owner != current_user.id:
            continue
        if _acquire_claim(r.id, current_user.id):
            recording = r
            break
    if not recording:
        return jsonify({'task': None})

    file_type = detect_modality(recording.filename)
    file_url = f'/uploads/{recording.filename}'

    # basic auto-timeline skeleton generated by duration
    timeline = []
    if recording.duration and recording.duration > 0:
        timeline = [{'start': 0, 'end': float(recording.duration), 'text': recording.text_content or ''}]

    return jsonify({
        'task': {
            'id': recording.id,
            'file_url': file_url,
            'filename': recording.filename,
            'modality': file_type,
            'duration': recording.duration or 0,
            'text': recording.text_content or '',
            'timeline': timeline,
            'requirements': '请完成转写/描述并校对时间轴，提交后进入待审核状态'
        }
    })


@app.route('/api/process/manual-save', methods=['POST'])
@login_required
def process_manual_save():
    """
    Save manual processing result for audio/video.
    body:
    {
      "recording_id": 1,
      "modality": "audio|video",
      "content_text": "...",
      "timeline": [{"start":0, "end": 5.2, "text":"..."}]
    }
    """
    # 反馈#7：手动处理为采集与处理人员动作
    if not _is_processor():
        return jsonify({'error': '仅数据采集与处理人员可保存处理结果'}), 403
    data = request.get_json(silent=True) or {}
    recording_id = data.get('recording_id')
    content_text = data.get('content_text', '')
    timeline = data.get('timeline', [])

    if not recording_id:
        return jsonify({'error': 'recording_id is required'}), 400
    try:
        recording_id = int(recording_id)
    except (TypeError, ValueError):
        return jsonify({'error': 'recording_id 非法'}), 400
    recording = Recording.query.get(recording_id)
    if not recording:
        return jsonify({'error': 'recording not found'}), 404

    if recording.status == 'pending_fix':
        pr = _latest_processing_result(recording.id)
        allowed = _is_system_admin() or (pr and pr.processor_id == current_user.id)
        if not allowed:
            return jsonify({'error': '无权重修该条打回数据'}), 403
    elif recording.status != 'pending':
        return jsonify({'error': '仅待处理或待修正状态可保存处理结果'}), 400

    # f3：领取锁校验——他人已领取正在处理的任务不可写
    owner = _claim_owner(recording.id)
    if owner is not None and owner != current_user.id:
        return jsonify({'error': '该任务正由其他人员处理中'}), 409
    _acquire_claim(recording.id, current_user.id)

    modality = data.get('modality') or detect_modality(recording.filename)
    # 反馈#8：新增 image 模态（图片描述/理解，BAGEL 图像理解）
    if modality not in {'audio', 'video', 'image'}:
        return jsonify({'error': 'unsupported modality'}), 400

    result_row = ProcessingResult(
        recording_id=recording.id,
        processor_id=current_user.id,
        modality=modality,
        process_mode='manual',
        content_text=content_text,
        timeline_json=json.dumps(timeline, ensure_ascii=False),
        processed_at=datetime.utcnow()
    )
    db.session.add(result_row)

    # write-back summary to recording and set waiting review
    recording.text_content = content_text
    if timeline:
        recording.subtitle_json_data = json.dumps(timeline, ensure_ascii=False)
    recording.status = 'pending_review'
    _sync_acquisition_audit(recording, 'pending')
    db.session.commit()
    _release_claim(recording.id, current_user.id)  # 任务离池，释放领取锁
    return jsonify({'success': True, 'message': '手动处理结果已写回，状态已更新为待审核'})


@app.route('/api/process/algorithm-submit', methods=['POST'])
@login_required
def process_algorithm_submit():
    """
    Submit algorithm task (BAGEL) and return async task_id.
    """
    # 反馈#7：算法模块调用为采集与处理人员动作
    if not _is_processor():
        return jsonify({'error': '仅数据采集与处理人员可调用算法处理'}), 403
    data = request.get_json(silent=True) or {}
    recording_id = data.get('recording_id')
    if not recording_id:
        return jsonify({'error': 'recording_id is required'}), 400
    recording = Recording.query.get(recording_id)
    if not recording:
        return jsonify({'error': 'recording not found'}), 404

    if not bagel_queue.health():
        return jsonify({'error': 'algorithm queue unavailable'}), 503

    try:
        task_id, backend, reused = _submit_algorithm_task(recording, data)
    except FatalBackendError as e:
        # 后端未配置 / payload 非法等不可重试错误：直接 400，不入队
        return jsonify({'error': str(e), 'code': 'backend_misconfigured'}), 400
    except (QuotaExceeded, CreditInsufficient) as e:
        return jsonify({'error': str(e), 'code': 'quota_exceeded'}), 429
    return jsonify({'success': True, 'task_id': task_id,
                    'status': 'done' if reused else 'queued',
                    'backend': backend, 'reused': reused})


@app.route('/api/process/algorithm-batch', methods=['POST'])
@login_required
def process_algorithm_batch():
    """反馈#16：批量算法处理——一次为多条待处理记录提交算法任务（单批最多 20 条）。
    每条独立计费/入队，单条失败不影响其余；前端轮询到完成后自动确认写回。
    body: {"recording_ids": [..], "backend": "auto|deepseek|local|autodl", "force_recompute": bool}
    """
    if not _is_processor():
        return jsonify({'error': '仅数据采集与处理人员可调用算法处理'}), 403
    data = request.get_json(silent=True) or {}
    raw_ids = data.get('recording_ids') or []
    if not isinstance(raw_ids, list):
        return jsonify({'error': 'recording_ids 必须为数组'}), 400
    ids = []
    try:
        for x in raw_ids:
            rid = int(x)
            if rid not in ids:
                ids.append(rid)
    except (TypeError, ValueError):
        return jsonify({'error': 'recording_ids 必须为整数数组'}), 400
    if not ids:
        return jsonify({'error': '请至少选择一条记录'}), 400
    if len(ids) > 20:
        return jsonify({'error': '单批最多 20 条，请分批提交'}), 400

    if not bagel_queue.health():
        return jsonify({'error': 'algorithm queue unavailable'}), 503

    items = []
    for rid in ids:
        recording = Recording.query.get(rid)
        if not recording:
            items.append({'recording_id': rid, 'status': 'error', 'error': '记录不存在或已删除'})
            continue
        try:
            task_id, backend, reused = _submit_algorithm_task(recording, data)
            items.append({'recording_id': rid,
                          'task_id': task_id,
                          'status': 'reused' if reused else 'queued',
                          'backend': backend,
                          'reused': bool(reused)})
        except FatalBackendError as e:
            # 后端未配置 / payload 非法：该条不入队，其余继续
            items.append({'recording_id': rid, 'status': 'error', 'error': str(e)})
        except (QuotaExceeded, CreditInsufficient) as e:
            items.append({'recording_id': rid, 'status': 'error', 'code': 'quota_exceeded',
                          'error': str(e)})
        except Exception as e:  # noqa: BLE001 - 批量接口需逐条容错
            db.session.rollback()
            app.logger.warning('algorithm batch submit failed for recording %s: %s', rid, e)
            items.append({'recording_id': rid, 'status': 'error', 'error': '提交异常：%s' % e})
    summary = {
        'total': len(items),
        'queued': sum(1 for i in items if i.get('status') == 'queued'),
        'reused': sum(1 for i in items if i.get('status') == 'reused'),
        'failed': sum(1 for i in items if i.get('status') == 'error'),
    }
    return jsonify({'success': True, 'items': items, 'summary': summary})


@app.route('/api/process/algorithm-confirm', methods=['POST'])
@login_required
def process_algorithm_confirm():
    """
    Confirm/correct algorithm result and persist.
    body:
    {
      "recording_id": 1,
      "task_id": "...",
      "content_text": "...",
      "timeline": [...]
    }
    """
    # 反馈#7：确认算法处理结果为采集与处理人员动作
    if not _is_processor():
        return jsonify({'error': '仅数据采集与处理人员可确认算法结果'}), 403
    data = request.get_json(silent=True) or {}
    recording_id = data.get('recording_id')
    task_id = data.get('task_id')
    if not recording_id or not task_id:
        return jsonify({'error': 'recording_id and task_id are required'}), 400

    recording = Recording.query.get(recording_id)
    if not recording:
        return jsonify({'error': 'recording not found'}), 404
    task = bagel_queue.get_task(task_id)
    if not task:
        return jsonify({'error': 'algorithm task not found'}), 404

    # f2 安全：越权/错配校验——任务必须已完成、任务 payload 必须指向该录音、
    # 非管理员只能确认自己提交的任务，防止用他人 task_id 确认/写回任意录音。
    if task.get('status') != 'done':
        return jsonify({'error': '任务尚未完成，无法确认', 'status': task.get('status')}), 409
    task_payload = task.get('payload') if isinstance(task.get('payload'), dict) else {}
    if task_payload.get('recording_id') != recording.id:
        return jsonify({'error': '任务与录音不匹配'}), 400
    owner_id = task_payload.get('submitted_by', task.get('submitted_by'))
    try:
        owner_id = int(owner_id) if owner_id is not None else None
    except (TypeError, ValueError):
        owner_id = None
    if not _is_system_admin() and owner_id is not None and owner_id != current_user.id:
        return jsonify({'error': '只能确认自己提交的算法任务'}), 403

    # 确认时兜底结算积分（正常情况下轮询时已结算）
    _settle_algorithm_billing(task)

    content_text = data.get('content_text')
    timeline = data.get('timeline')
    if content_text is None:
        # fallback to algorithm result
        result = task.get('result') if isinstance(task.get('result'), dict) else {}
        content_text = result.get('content_text', '')
    if timeline is None:
        result = task.get('result') if isinstance(task.get('result'), dict) else {}
        timeline = result.get('timeline', [])

    result_row = ProcessingResult(
        recording_id=recording.id,
        processor_id=current_user.id,
        modality=detect_modality(recording.filename),
        process_mode='algorithm',
        content_text=content_text or '',
        timeline_json=json.dumps(timeline or [], ensure_ascii=False),
        algorithm_raw_json=json.dumps(task.get('result') or {}, ensure_ascii=False),
        processed_at=datetime.utcnow()
    )
    db.session.add(result_row)

    recording.text_content = content_text or ''
    if timeline:
        recording.subtitle_json_data = json.dumps(timeline, ensure_ascii=False)
    recording.status = 'pending_review'
    _sync_acquisition_audit(recording, 'pending')
    db.session.commit()
    return jsonify({'success': True, 'message': '算法处理结果已确认写回，状态已更新为待审核'})


# --- 数据审核模块（待审核队列 / 预览 / 通过 / 打回 / 审核员自行修正 / 留痕） ---


@app.route('/audit')
@login_required
def audit_page():
    if not _is_reviewer():
        flash('仅审核人员或管理员可访问数据审核模块。', 'danger')
        return redirect(url_for('index'))
    lang = get_language()
    return render_template(f'audit_{lang}.html')


@app.route('/api/audit/pending-list')
@login_required
def audit_pending_list():
    if not _is_reviewer():
        return jsonify({'error': 'Forbidden'}), 403
    rows = (
        Recording.query.filter(Recording.status == 'pending_review')
        .filter(Recording.deleted_at.is_(None))
        .order_by(Recording.created_at.desc())
        .limit(200)
        .all()
    )
    items = []
    for r in rows:
        pr = _latest_processing_result(r.id)
        pu = User.query.get(pr.processor_id) if pr else None
        uu = None
        if r.uploader_id:
            uu_obj = User.query.get(r.uploader_id)
            uu = uu_obj.username if uu_obj else None
        elif r.recorded_by:
            uu_obj = User.query.get(r.recorded_by)
            uu = uu_obj.username if uu_obj else None
        # 反馈#9 需求2：队列项补充数据类型/格式/大小/时长/来源等基本信息
        modality = detect_modality(r.filename)
        file_ext = r.filename.rsplit('.', 1)[1].upper() if '.' in r.filename else ''
        items.append(
            {
                'id': r.id,
                'filename': r.filename,
                'modality': modality,
                'file_type': r.file_type or modality,
                'file_ext': file_ext,
                'file_size': r.file_size,
                'duration': r.duration,
                'source_channel': r.source_channel,
                'task_no': r.acquisition_task_no,
                'uploader_username': uu,
                'processor_username': pu.username if pu else None,
                'created_at': r.created_at.isoformat() if r.created_at else None,
            }
        )
    return jsonify({'items': items})


@app.route('/api/admin/recordings/in-progress')
@login_required
def admin_recordings_in_progress():
    """反馈#9 需求3b：超管/管理员在办数据总览——待处理/打回待修正/待审核的数据，
    含当前持锁处理人（Redis 领取锁）与最近审核查看人（review_seen 留痕）。"""
    if not _is_system_admin():
        return jsonify({'error': 'Forbidden'}), 403
    rows = (
        Recording.query.filter(Recording.status.in_(['pending', 'pending_fix', 'pending_review']))
        .filter(Recording.deleted_at.is_(None))
        .order_by(Recording.created_at.desc())
        .limit(300)
        .all()
    )
    user_cache = {}

    def uname(uid):
        if not uid:
            return None
        if uid not in user_cache:
            u = User.query.get(uid)
            user_cache[uid] = u.username if u else ('用户%s' % uid)
        return user_cache[uid]

    items = []
    counts = {'pending': 0, 'pending_fix': 0, 'pending_review': 0}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
        pr = _latest_processing_result(r.id)
        owner_id = _claim_owner(r.id)
        reviewer_name = None
        seen = (
            AuditLog.query.filter_by(recording_id=r.id, action_type='review_seen')
            .order_by(AuditLog.created_at.desc())
            .first()
        )
        if seen:
            reviewer_name = uname(seen.auditor_id)
        uploader_id = r.uploader_id or r.recorded_by
        items.append(
            {
                'id': r.id,
                'filename': r.filename,
                'modality': detect_modality(r.filename),
                'file_type': r.file_type or detect_modality(r.filename),
                'status': r.status,
                'uploader': uname(uploader_id),
                'processor': uname(pr.processor_id) if pr else None,
                'claim_owner': uname(owner_id),
                'reviewer': reviewer_name,
                'task_no': r.acquisition_task_no,
                'created_at': r.created_at.isoformat() if r.created_at else None,
            }
        )
    return jsonify({'counts': counts, 'total': len(items), 'items': items})


@app.route('/api/audit/next')
@login_required
def audit_next():
    if not _is_reviewer():
        return jsonify({'error': 'Forbidden'}), 403
    recording = (
        Recording.query.filter(Recording.status == 'pending_review')
        .filter(Recording.deleted_at.is_(None))
        .order_by(Recording.created_at.desc())
        .first()
    )
    if not recording:
        return jsonify({'task': None})
    _mark_review_seen(recording)
    return jsonify({'task': _build_audit_preview(recording)})


@app.route('/api/audit/detail/<int:recording_id>')
@login_required
def audit_detail(recording_id):
    if not _is_reviewer():
        return jsonify({'error': 'Forbidden'}), 403
    recording = Recording.query.get(recording_id)
    if not recording:
        return jsonify({'error': 'not found'}), 404
    if recording.deleted_at is not None:
        return jsonify({'error': '该文件已在垃圾箱中'}), 400
    if recording.status != 'pending_review':
        return jsonify({'error': '该条不在待审核队列'}), 400
    _mark_review_seen(recording)
    return jsonify({'task': _build_audit_preview(recording)})


@app.route('/api/audit/pass', methods=['POST'])
@login_required
def audit_pass():
    if not _is_reviewer():
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json(silent=True) or {}
    recording_id = data.get('recording_id')
    if not recording_id:
        return jsonify({'error': 'recording_id is required'}), 400
    recording = Recording.query.get(recording_id)
    if not recording or recording.status != 'pending_review':
        return jsonify({'error': '录音不存在或不在待审核状态'}), 404

    recording.status = 'approved'
    if data.get('review_remark') is not None:
        recording.review_remark = (data.get('review_remark') or '').strip()
    _sync_acquisition_audit(recording, 'pass')
    _append_audit_log(
        recording.id,
        'pass',
        'approved',
        comments=data.get('comments', ''),
        detail={'note': 'audit approved and archived (logical compliance store)',
                'review_remark': recording.review_remark or ''},
    )
    db.session.commit()
    return jsonify({'success': True, 'message': '审核通过，已标记为合规并归档（逻辑归档）'})


@app.route('/api/audit/reject-return', methods=['POST'])
@login_required
def audit_reject_return():
    """打回修正：待修正 + 站内消息通知原处理人。"""
    if not _is_reviewer():
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json(silent=True) or {}
    recording_id = data.get('recording_id')
    comments = (data.get('comments') or '').strip()
    if not recording_id:
        return jsonify({'error': 'recording_id is required'}), 400
    if not comments:
        return jsonify({'error': '请填写具体、可落地的审核意见'}), 400
    recording = Recording.query.get(recording_id)
    if not recording or recording.status != 'pending_review':
        return jsonify({'error': '录音不存在或不在待审核状态'}), 404

    pr = _latest_processing_result(recording.id)
    processor_id = pr.processor_id if pr else recording.recorded_by

    recording.status = 'pending_fix'
    if data.get('review_remark') is not None:
        recording.review_remark = (data.get('review_remark') or '').strip()
    _sync_acquisition_audit(recording, 'pending_fix')
    _append_audit_log(
        recording.id,
        'reject_return',
        'pending_fix',
        comments=comments,
        detail={'processor_id': processor_id, 'review_remark': recording.review_remark or ''},
    )
    _send_audit_reject_message(recording, processor_id, comments)
    db.session.commit()
    return jsonify({'success': True, 'message': '已打回修正，并已通知原处理人'})


@app.route('/api/audit/self-fix', methods=['POST'])
@login_required
def audit_self_fix():
    """审核员自行修正：写回转写/描述与时间轴后自动按通过处理。"""
    if not _is_reviewer():
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json(silent=True) or {}
    recording_id = data.get('recording_id')
    content_text = data.get('content_text')
    timeline = data.get('timeline')
    if not recording_id:
        return jsonify({'error': 'recording_id is required'}), 400
    if content_text is None:
        return jsonify({'error': 'content_text is required'}), 400
    recording = Recording.query.get(recording_id)
    if not recording or recording.status != 'pending_review':
        return jsonify({'error': '录音不存在或不在待审核状态'}), 404

    if timeline is None:
        pr0 = _latest_processing_result(recording.id)
        if pr0 and pr0.timeline_json:
            try:
                timeline = json.loads(pr0.timeline_json)
            except (json.JSONDecodeError, TypeError):
                timeline = []
        else:
            timeline = []

    modality = detect_modality(recording.filename)
    if modality == 'unknown':
        modality = 'video'

    result_row = ProcessingResult(
        recording_id=recording.id,
        processor_id=current_user.id,
        modality=modality,
        process_mode='reviewer_self_fix',
        content_text=content_text,
        timeline_json=json.dumps(timeline or [], ensure_ascii=False),
        processed_at=datetime.utcnow(),
    )
    db.session.add(result_row)
    recording.text_content = content_text
    if timeline:
        recording.subtitle_json_data = json.dumps(timeline or [], ensure_ascii=False)
    recording.status = 'approved'
    if data.get('review_remark') is not None:
        recording.review_remark = (data.get('review_remark') or '').strip()
    _sync_acquisition_audit(recording, 'pass')
    _append_audit_log(
        recording.id,
        'self_fix',
        'approved',
        comments=data.get('comments', ''),
        detail={'timeline_segments': len(timeline or []), 'review_remark': recording.review_remark or ''},
    )
    db.session.commit()
    return jsonify({'success': True, 'message': '已保存修正并通过审核'})


@app.route('/api/audit/algorithm-recheck', methods=['POST'])
@login_required
def audit_algorithm_recheck():
    """审核环节算法复核（反馈#7）：DeepSeek 同步调用，输出结构化复核意见供审核员参考。
    autoDL/局域网 GPU 接口预留（未配置前返回明确提示）。复核不改变录音审核状态，最终判定由审核员做出。"""
    if not _is_reviewer():
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json(silent=True) or {}
    recording_id = data.get('recording_id')
    backend = (data.get('backend') or 'deepseek').strip().lower()
    if not recording_id:
        return jsonify({'error': 'recording_id is required'}), 400
    recording = Recording.query.get(recording_id)
    if not recording or recording.deleted_at is not None:
        return jsonify({'error': '录音不存在'}), 404
    if recording.status != 'pending_review':
        return jsonify({'error': '该条不在待审核状态，无法复核'}), 400

    if backend in ('autodl', 'local'):
        return jsonify({
            'error': f'{backend} 后端接口尚未配置（入口已预留），请先使用 deepseek 复核；'
                     f'待管理员在用户管理中配置 GPU 服务地址后自动启用。'
        }), 400
    if backend not in ('deepseek', 'auto', ''):
        return jsonify({'error': '不支持的后端'}), 400

    # 组装复核输入（文本 + 时间轴；纯文本模型看不到音视频本体）
    pr = _latest_processing_result(recording.id)
    timeline = []
    if pr and pr.timeline_json:
        try:
            timeline = json.loads(pr.timeline_json)
        except (json.JSONDecodeError, TypeError):
            timeline = []
    system_prompt = (data.get('system_prompt') or '').strip() or algorithm_prompts.DEEPSEEK_RECHECK_SYSTEM_PROMPT
    extra = (data.get('prompt_extra') or '').strip()
    user_prompt = algorithm_prompts.build_recheck_prompt(
        text=recording.text_content or '', timeline=timeline,
        modality=detect_modality(recording.filename),
        filename=recording.filename, extra=extra,
    )
    payload = {
        'recording_id': recording.id,
        'filename': recording.filename,
        'modality': detect_modality(recording.filename),
        'submitted_by': current_user.id,
        'backend': BACKEND_DEEPSEEK,
        'options': {
            'system_prompt': system_prompt,
            'prompt': user_prompt,
            'json_mode': True,
            'temperature': 0.2,
        },
    }

    # 计费：预估冻结 → 同步调用 → 按实际 token 结算（与队列任务同一套积分规则；超管免费）
    est_cost, _, _ = credit_service.estimate_cost(BACKEND_DEEPSEEK, recording, payload, Config)
    try:
        billing = credit_service.freeze(current_user.id, est_cost, Config, model_redis)
    except CreditInsufficient as e:
        return jsonify({'error': str(e)}), 429
    task_id = f'recheck-{recording.id}-{uuid.uuid4().hex[:12]}'

    def _refund_held():
        if billing == 'held':
            credit_service.refund(current_user.id, est_cost, model_redis)

    try:
        result = call_deepseek(payload, Config, model_redis)
    except QuotaExceeded as e:
        _refund_held()
        return jsonify({'error': str(e)}), 429
    except FatalBackendError as e:
        _refund_held()
        # 未配置 Key / 4xx 等不可重试错误：明确提示，不写失败流水（未产生调用）
        return jsonify({'error': f'算法复核不可用：{e}'}), 400
    except Exception as e:
        app.logger.warning('audit recheck deepseek failed: %s', e, exc_info=True)
        _refund_held()
        try:
            credit_service._write_usage(task_id, current_user.id, BACKEND_DEEPSEEK, recording, payload,
                                        0, est_cost, 'tokens', 0, 'failed', f'审核复核失败: {str(e)[:200]}')
            db.session.commit()
        except Exception:
            db.session.rollback()
        return jsonify({'error': '算法复核服务异常（DeepSeek 调用失败），请稍后重试或联系管理员'}), 502

    tokens = float(result.get('tokens_used') or 0)
    cost, _, _ = credit_service.actual_cost(BACKEND_DEEPSEEK, result, recording, payload, Config)
    charged = cost if billing == 'held' else 0
    if billing == 'held':
        diff = est_cost - cost
        if diff != 0:
            try:
                model_redis.incrby(credit_service.balance_key(current_user.id), diff)
            except _redis_lib.RedisError:
                pass
    detail = '审核环节算法复核（DeepSeek 同步调用）'
    if billing != 'held':
        detail += '；未冻结积分（管控未生效/超管免费），计 0 积分'
    credit_service._write_usage(task_id, current_user.id, BACKEND_DEEPSEEK, recording, payload,
                                charged, est_cost, 'tokens', tokens, 'success', detail)
    _append_audit_log(recording.id, 'algorithm_recheck', 'recheck_done',
                      comments=extra[:500],
                      detail={'backend': 'deepseek', 'tokens_used': tokens,
                              'verdict': result.get('verdict'), 'cost_credits': charged,
                              'task_id': task_id})
    db.session.commit()
    return jsonify({
        'success': True,
        'backend': 'deepseek',
        'tokens_used': tokens,
        'cost_credits': charged,
        'verdict': result.get('verdict') or 'revise',
        'confidence': result.get('confidence'),
        'issues': result.get('issues') or [],
        'corrected_text': result.get('corrected_text') or '',
        'summary': result.get('summary') or '',
    })


@app.route('/api/audit/logs/<int:recording_id>')
@login_required
def audit_logs_for_recording(recording_id):
    if not _is_reviewer():
        return jsonify({'error': 'Forbidden'}), 403
    rows = (
        AuditLog.query.filter_by(recording_id=recording_id)
        .order_by(AuditLog.created_at.desc())
        .limit(100)
        .all()
    )
    out = []
    for row in rows:
        auditor = User.query.get(row.auditor_id)
        out.append(
            {
                'id': row.id,
                'auditor': auditor.username if auditor else str(row.auditor_id),
                'action_type': row.action_type,
                'audit_result': row.audit_result,
                'comments': row.comments,
                'detail_json': row.detail_json,
                'created_at': row.created_at.isoformat() if row.created_at else None,
            }
        )
    return jsonify({'items': out})


@app.route('/messages')
@login_required
def messages_page():
    lang = get_language()
    return render_template(f'messages_{lang}.html')


@app.route('/api/messages/my')
@login_required
def my_audit_messages():
    """兼容旧接口：仅审核类历史站内信（表精简后统一走 inbox_messages，category='audit'）。"""
    rows = (
        InboxMessage.query.filter(
            InboxMessage.recipient_id == current_user.id,
            InboxMessage.category == 'audit',
            InboxMessage.user_deleted_at.is_(None),
        )
        .order_by(InboxMessage.created_at.desc())
        .limit(50)
        .all()
    )
    items = []
    for m in rows:
        items.append(
            {
                'id': m.id,
                'title': m.title,
                'body': m.body,
                'recording_id': m.business_id if m.business_type == 'recording' else None,
                'read_at': m.read_at.isoformat() if m.read_at else None,
                'created_at': m.created_at.isoformat() if m.created_at else None,
            }
        )
    return jsonify({'items': items})


@app.route('/api/messages/inbox', methods=['GET'])
@login_required
def api_messages_inbox():
    read_filter = request.args.get('read', 'all')
    category = request.args.get('category')
    qtext = (request.args.get('q') or '').strip()
    business_id = (request.args.get('business_id') or '').strip()
    sort_mode = request.args.get('sort', 'time')
    include_legacy = request.args.get('include_legacy', '1') == '1'
    df = _parse_task_datetime(request.args.get('from'))
    dt = _parse_task_datetime(request.args.get('to'))

    q = InboxMessage.query.filter(
        InboxMessage.recipient_id == current_user.id,
        InboxMessage.user_deleted_at.is_(None),
    )
    if read_filter == 'read':
        q = q.filter(InboxMessage.read_at.isnot(None))
    elif read_filter == 'unread':
        q = q.filter(InboxMessage.read_at.is_(None))
    if category:
        q = q.filter(InboxMessage.category == category)
    if business_id:
        q = q.filter(InboxMessage.business_id == business_id)
    if df:
        q = q.filter(InboxMessage.created_at >= df)
    if dt:
        q = q.filter(InboxMessage.created_at <= dt)
    if qtext:
        like = f'%{qtext}%'
        q = q.filter(or_(InboxMessage.title.like(like), InboxMessage.body.like(like)))
    rows = q.all()

    items = [_serialize_inbox_row(m) for m in rows]
    # 表精简后：旧 audit_messages / task_messages 数据已由迁移脚本并入 inbox_messages，不再合并查询

    def _ts_key(s):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace('Z', '+00:00').split('+')[0])
        except ValueError:
            return None

    def _ts_num(t):
        if not t:
            return 0.0
        try:
            return -float(t.timestamp())
        except (OSError, OverflowError, ValueError):
            return 0.0

    def _sort_key(it):
        pin = 0 if it.get('pinned_at') else 1
        unread = 0 if not it.get('read_at') else 1
        pr = _MSG_PRIO.get(it.get('priority'), 2)
        t = _ts_key(it.get('created_at'))
        ts = _ts_num(t)
        if sort_mode == 'priority':
            return (pin, unread, -pr, ts)
        return (pin, unread, ts, -pr)

    items.sort(key=_sort_key)

    return jsonify({'items': items})


@app.route('/api/messages/unread-count', methods=['GET'])
@login_required
def api_messages_unread_count():
    n_inbox = (
        InboxMessage.query.filter(
            InboxMessage.recipient_id == current_user.id,
            InboxMessage.user_deleted_at.is_(None),
            InboxMessage.read_at.is_(None),
        ).count()
    )
    # 表精简后：未读统一为 inbox_messages 计数（旧 audit_messages/task_messages 已并入）
    return jsonify({'count': n_inbox, 'inbox': n_inbox})


@app.route('/api/messages/inbox/<int:message_id>/read', methods=['POST'])
@login_required
def api_inbox_mark_read(message_id):
    m = InboxMessage.query.get(message_id)
    if not m or m.recipient_id != current_user.id or m.user_deleted_at:
        return jsonify({'error': 'not found'}), 404
    m.read_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/messages/inbox/read-all', methods=['POST'])
@login_required
def api_inbox_mark_all_read():
    now = datetime.utcnow()
    InboxMessage.query.filter(
        InboxMessage.recipient_id == current_user.id,
        InboxMessage.user_deleted_at.is_(None),
        InboxMessage.read_at.is_(None),
    ).update({InboxMessage.read_at: now}, synchronize_session=False)
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/messages/inbox/<int:message_id>/pin', methods=['POST'])
@login_required
def api_inbox_pin(message_id):
    m = InboxMessage.query.get(message_id)
    if not m or m.recipient_id != current_user.id or m.user_deleted_at:
        return jsonify({'error': 'not found'}), 404
    data = request.get_json(silent=True) or {}
    if data.get('pinned', True):
        m.pinned_at = datetime.utcnow()
    else:
        m.pinned_at = None
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/messages/inbox/<int:message_id>', methods=['DELETE'])
@login_required
def api_inbox_soft_delete(message_id):
    m = InboxMessage.query.get(message_id)
    if not m or m.recipient_id != current_user.id:
        return jsonify({'error': 'not found'}), 404
    m.user_deleted_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/messages/inbox/batch-delete', methods=['POST'])
@login_required
def api_inbox_batch_delete():
    data = request.get_json(silent=True) or {}
    ids = data.get('ids') or []
    now = datetime.utcnow()
    for mid in ids:
        try:
            iid = int(mid)
        except (TypeError, ValueError):
            continue
        m = InboxMessage.query.get(iid)
        if m and m.recipient_id == current_user.id:
            m.user_deleted_at = now
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/messages/legacy/read', methods=['POST'])
@login_required
def api_messages_legacy_read():
    """兼容旧前端：旧 audit/task 消息已迁入 inbox_messages，按统一 id 标记已读。"""
    data = request.get_json(silent=True) or {}
    mid = data.get('id')
    try:
        mid = int(mid)
    except (TypeError, ValueError):
        return jsonify({'error': 'bad id'}), 400
    m = InboxMessage.query.get(mid)
    if not m or m.recipient_id != current_user.id or m.user_deleted_at:
        return jsonify({'error': 'not found'}), 404
    m.read_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/messages/<int:message_id>/read', methods=['POST'])
@login_required
def mark_message_read(message_id):
    m = InboxMessage.query.get(message_id)
    if not m or m.recipient_id != current_user.id or m.user_deleted_at:
        return jsonify({'error': 'not found'}), 404
    m.read_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/admin/message-templates', methods=['GET'])
@login_required
def admin_message_templates_list():
    if not _is_system_admin():
        return jsonify({'error': 'Forbidden'}), 403
    rows = MessageTemplate.query.order_by(MessageTemplate.template_key.asc()).all()
    return jsonify(
        {
            'items': [
                {
                    'template_key': r.template_key,
                    'category': r.category,
                    'title_template': r.title_template,
                    'body_template': r.body_template,
                    'enabled': r.enabled,
                    'default_priority': r.default_priority,
                    'description': r.description,
                }
                for r in rows
            ]
        }
    )


@app.route('/api/admin/message-templates/<template_key>', methods=['PUT'])
@login_required
def admin_message_templates_put(template_key):
    if not _is_system_admin():
        return jsonify({'error': 'Forbidden'}), 403
    r = MessageTemplate.query.filter_by(template_key=template_key).first_or_404()
    data = request.get_json(silent=True) or {}
    if 'title_template' in data:
        r.title_template = data['title_template'] or r.title_template
    if 'body_template' in data:
        r.body_template = data['body_template'] or r.body_template
    if 'enabled' in data:
        r.enabled = bool(data['enabled'])
    if 'default_priority' in data:
        r.default_priority = data['default_priority'][:16]
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/admin/message-channels', methods=['GET', 'PUT'])
@login_required
def admin_message_channels():
    if not _is_system_admin():
        return jsonify({'error': 'Forbidden'}), 403
    from services.messaging import get_or_create_channel_config

    cfg = get_or_create_channel_config()
    if request.method == 'GET':
        try:
            roles = json.loads(cfg.email_roles_json or '[]')
        except (json.JSONDecodeError, TypeError):
            roles = []
        return jsonify(
            {
                'email_enabled': cfg.email_enabled,
                'sms_enabled': cfg.sms_enabled,
                'email_roles': roles,
            }
        )
    data = request.get_json(silent=True) or {}
    if 'email_enabled' in data:
        cfg.email_enabled = bool(data['email_enabled'])
    if 'sms_enabled' in data:
        cfg.sms_enabled = bool(data['sms_enabled'])
    if 'email_roles' in data:
        cfg.email_roles_json = json.dumps(data['email_roles'], ensure_ascii=False)
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/admin/announcements', methods=['GET', 'POST'])
@login_required
def admin_announcements():
    if not _is_system_admin():
        return jsonify({'error': 'Forbidden'}), 403
    if request.method == 'GET':
        rows = SystemAnnouncement.query.order_by(SystemAnnouncement.created_at.desc()).limit(100).all()
        return jsonify(
            {
                'items': [
                    {
                        'id': x.id,
                        'title': x.title,
                        'body_html': x.body_html,
                        'priority': x.priority,
                        'valid_from': x.valid_from.isoformat() if x.valid_from else None,
                        'valid_until': x.valid_until.isoformat() if x.valid_until else None,
                        'audience_json': x.audience_json,
                        'published_at': x.published_at.isoformat() if x.published_at else None,
                        'is_active': x.is_active,
                        'created_at': x.created_at.isoformat() if x.created_at else None,
                    }
                    for x in rows
                ]
            }
        )
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    body_html = data.get('body_html') or ''
    if not title:
        return jsonify({'error': '标题必填'}), 400
    aud = data.get('audience') or {'mode': 'all'}
    ann = SystemAnnouncement(
        title=title[:255],
        body_html=body_html,
        priority=(data.get('priority') or 'medium')[:16],
        valid_from=_parse_task_datetime(data.get('valid_from')),
        valid_until=_parse_task_datetime(data.get('valid_until')),
        audience_json=json.dumps(aud, ensure_ascii=False),
        created_by=current_user.id,
        is_active=bool(data.get('is_active', True)),
    )
    db.session.add(ann)
    db.session.commit()
    return jsonify({'success': True, 'id': ann.id})


@app.route('/api/admin/announcements/<int:aid>/publish', methods=['POST'])
@login_required
def admin_announcements_publish(aid):
    if not _is_system_admin():
        return jsonify({'error': 'Forbidden'}), 403
    from services.messaging import publish_announcement_fanout

    ann = SystemAnnouncement.query.get_or_404(aid)
    n = publish_announcement_fanout(ann.id)
    if n < 0:
        # f3：公告发布幂等，重复发布返回冲突提示
        return jsonify({'error': '该公告已发布，请勿重复发布（如需重发请新建公告）'}), 409
    db.session.commit()
    return jsonify({'success': True, 'recipients': n})


@app.route('/api/admin/message-delivery-log', methods=['GET'])
@login_required
def admin_message_delivery_log():
    if not _is_system_admin():
        return jsonify({'error': 'Forbidden'}), 403
    uid = request.args.get('recipient_id', type=int)
    ch = request.args.get('channel')
    q = MessageDeliveryLog.query
    if uid:
        q = q.filter(MessageDeliveryLog.recipient_id == uid)
    if ch:
        q = q.filter(MessageDeliveryLog.channel == ch)
    rows = q.order_by(MessageDeliveryLog.created_at.desc()).limit(500).all()
    return jsonify(
        {
            'items': [
                {
                    'id': x.id,
                    'inbox_message_id': x.inbox_message_id,
                    'recipient_id': x.recipient_id,
                    'msg_type': x.msg_type,
                    'channel': x.channel,
                    'status': x.status,
                    'title_snapshot': x.title_snapshot,
                    'detail': x.detail,
                    'created_at': x.created_at.isoformat() if x.created_at else None,
                }
                for x in rows
            ]
        }
    )


@app.route('/api/admin/message-delivery-log/export', methods=['GET'])
@login_required
def admin_message_delivery_log_export():
    if not _is_system_admin():
        return jsonify({'error': 'Forbidden'}), 403
    rows = MessageDeliveryLog.query.order_by(MessageDeliveryLog.created_at.desc()).limit(5000).all()
    payload = [
        {
            'id': x.id,
            'inbox_message_id': x.inbox_message_id,
            'recipient_id': x.recipient_id,
            'sender_type': x.sender_type,
            'sender_id': x.sender_id,
            'msg_type': x.msg_type,
            'channel': x.channel,
            'status': x.status,
            'title_snapshot': x.title_snapshot,
            'detail': x.detail,
            'created_at': x.created_at.isoformat() if x.created_at else None,
        }
        for x in rows
    ]
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        mimetype='application/json; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=message_delivery_log.json'},
    )


@app.route('/api/admin/messages/scan-deadlines', methods=['POST'])
@login_required
def admin_messages_scan_deadlines():
    if not _is_system_admin():
        return jsonify({'error': 'Forbidden'}), 403
    from services.messaging import scan_task_deadline_warnings

    stats = scan_task_deadline_warnings()
    db.session.commit()
    return jsonify({'success': True, **stats})


# Serve uploaded files
@app.route('/uploads/<filename>')
@login_required
def uploaded_file(filename):
    # f3：send_from_directory 内置 safe_join，防御路径穿越
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# 3. Data Management Module
@app.route('/manage')
@login_required
def manage():
    # 反馈#7 权限矩阵：全量数据管理仅系统管理员/超级管理员（inspector 的无效数据处置在审核页）
    if not _is_system_admin():
        flash('数据全量管理模块仅系统管理员/超级管理员可访问。', 'danger')
        return redirect(url_for('index'))
    recordings = Recording.query.all()
    lang = get_language()
    return render_template(f'manage_{lang}.html', recordings=recordings)

# 4. Task Management Module
@app.route('/tasks')
@login_required
def tasks():
    tasks = Task.query.all()
    lang = get_language()
    return render_template(f'tasks_{lang}.html', tasks=tasks)

# 5. User Management Module
@app.route('/users')
@login_required
def users():
    if not _is_system_admin():
        flash('无权访问用户管理', 'danger')
        return redirect(url_for('index'))
    users = User.query.all()
    lang = get_language()
    return render_template(
        f'users_{lang}.html',
        users=users,
        can_assign_super_admin=_is_super_admin(),
    )

# API Endpoints for streaming video upload
@app.route('/api/upload/stream', methods=['POST'])
@login_required
def stream_upload():
    # 反馈#7 权限矩阵：分片上传同样仅采集与处理人员
    if not _is_processor():
        return jsonify({'error': '仅数据采集与处理人员可上传数据'}), 403
    # f3：分片上传健壮性——参数/范围校验、缺片先检测、uuid 落盘、先写 .tmp 再原子替换、
    # 合并后清理分片、异常不回传内部错误信息
    try:
        if 'chunk' not in request.files:
            return jsonify({'error': '缺少分片文件 chunk'}), 400
        chunk = request.files['chunk']
        try:
            chunk_number = int(request.form.get('chunk_number', ''))
            total_chunks = int(request.form.get('total_chunks', ''))
        except (TypeError, ValueError):
            return jsonify({'error': 'chunk_number/total_chunks 必须为整数'}), 400
        original_filename = request.form.get('filename') or ''
        if total_chunks < 1 or total_chunks > 5000:
            return jsonify({'error': 'total_chunks 超出允许范围(1-5000)'}), 400
        if chunk_number < 0 or chunk_number >= total_chunks:
            return jsonify({'error': 'chunk_number 超出范围'}), 400

        # 分片临时名只做危险字符清理；最终文件名统一由服务端生成 uuid
        def clean_filename(name):
            dangerous_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|']
            for ch in dangerous_chars:
                name = name.replace(ch, '_')
            return name.strip() or f'upload_{int(time.time())}'

        part_key = clean_filename(original_filename)
        if not allowed_file(part_key):
            return jsonify({'error': '不支持的文件类型'}), 400

        temp_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'temp', str(current_user.id))
        os.makedirs(temp_dir, exist_ok=True)

        chunk_path = os.path.join(temp_dir, f"{part_key}.part{chunk_number}")
        chunk.save(chunk_path)

        if chunk_number == total_chunks - 1:
            part_paths = [os.path.join(temp_dir, f"{part_key}.part{i}") for i in range(total_chunks)]
            # 合并前先检查分片齐全，避免缺片时截断/产出残文件
            missing = [i for i, p in enumerate(part_paths) if not os.path.exists(p)]
            if missing:
                app.logger.warning('stream_upload 缺片: file=%s missing=%s', part_key, missing[:20])
                return jsonify({'error': f'分片不完整，缺少 {len(missing)} 个分片，请重新上传'}), 400

            storage_name, _ext = _unique_storage_name(part_key)
            if not storage_name:
                return jsonify({'error': '不支持的文件类型'}), 400
            final_path = os.path.join(app.config['UPLOAD_FOLDER'], storage_name)
            tmp_path = final_path + '.tmp'
            try:
                with open(tmp_path, 'wb') as outfile:
                    for p in part_paths:
                        with open(p, 'rb') as infile:
                            while True:
                                buf = infile.read(8 * 1024 * 1024)
                                if not buf:
                                    break
                                outfile.write(buf)
                os.replace(tmp_path, final_path)
            except OSError:
                app.logger.exception('stream_upload 合并分片失败: %s', part_key)
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                return jsonify({'error': '分片合并失败，请重新上传'}), 500
            finally:
                for p in part_paths:
                    try:
                        os.remove(p)
                    except OSError:
                        pass

            # 反馈#12：录制/采集编号——前端预生成（时间戳+随机后缀）可修改，
            # 服务端做格式与唯一性权威校验；重复编号拒绝入库并清理已合并文件。
            task_no = (request.form.get('task_no') or '').strip()
            if task_no:
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{3,62}', task_no):
                    try:
                        os.remove(final_path)
                    except OSError:
                        pass
                    return jsonify({'error': '录制编号格式无效：需 4-63 位，以字母/数字开头，仅含字母、数字、中划线、下划线'}), 400
                dup_no = Recording.query.filter(
                    Recording.acquisition_task_no == task_no,
                    Recording.deleted_at.is_(None),
                ).first()
                if dup_no:
                    try:
                        os.remove(final_path)
                    except OSError:
                        pass
                    return jsonify({'error': f'录制编号 {task_no} 已存在（数据 #{dup_no.id}），请更换编号后重新上传',
                                    'code': 'duplicate_task_no'}), 409

            # Create recording entry
            recording = Recording(
                filename=storage_name,
                file_path=final_path,
                recorded_by=current_user.id,
                status='pending'
            )
            db.session.add(recording)
            db.session.flush()  # get recording id before metadata
            fill_acquisition_fields(recording, current_user.id, source_channel='record')
            if task_no:
                recording.acquisition_task_no = task_no
            db.session.commit()

            app.logger.info('Upload completed: recording_id=%s task_no=%s', recording.id, recording.acquisition_task_no)
            return jsonify({'success': True, 'recording_id': recording.id,
                            'task_no': recording.acquisition_task_no})

        return jsonify({'success': True, 'chunk_received': chunk_number})
    except Exception:
        app.logger.exception('stream_upload unexpected error')
        return jsonify({'error': '上传失败，请重试'}), 500


@app.route('/api/upload/check-no')
@login_required
def check_collect_no():
    """反馈#12：录制/采集编号实时预校验（输入框失焦/上传前调用）；服务端查重为最终权威。"""
    if not _is_processor():
        return jsonify({'error': '仅数据采集与处理人员可使用'}), 403
    tn = (request.args.get('task_no') or '').strip()
    if not tn:
        return jsonify({'available': False, 'reason': 'empty'})
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{3,62}', tn):
        return jsonify({'available': False, 'reason': '格式需 4-63 位，以字母/数字开头，仅含字母、数字、中划线、下划线'})
    dup = Recording.query.filter(
        Recording.acquisition_task_no == tn,
        Recording.deleted_at.is_(None),
    ).first()
    if dup:
        return jsonify({'available': False, 'reason': f'编号已存在（数据 #{dup.id}），请更换'})
    return jsonify({'available': True})

# API Endpoints for Data Management
@app.route('/api/dashboard/stats')
@login_required
def dashboard_stats():
    return jsonify(compute_dashboard_stats())


@app.route('/api/manage/test')
@login_required
def test_manage_data():
    if not _can_access_manage_data():
        return jsonify({'error': '无权访问数据管理'}), 403
    try:
        total_recordings = Recording.query.count()
        total_users = User.query.count()
        return jsonify(
            {
                'status': 'ok',
                'total_recordings': total_recordings,
                'total_users': total_users,
                'current_user': current_user.username if current_user else 'None',
            }
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/manage/data')
@login_required
def get_manage_data():
    if not _can_access_manage_data():
        return jsonify({'error': '无权访问数据管理'}), 403
    try:
        args = normalize_manage_args(request.args.to_dict(flat=True))
        # 反馈#6：查看垃圾箱时懒清理过期记录（超过 30 天物理删除）
        if str(args.get('trash_only', '')).lower() in ('1', 'true', 'yes'):
            try:
                _purge_expired_trash()
            except Exception:
                app.logger.warning('垃圾箱过期懒清理失败', exc_info=True)
        page = max(1, int(args.get('page', 1)))
        per_page = int(args.get('per_page', 10))
        if per_page not in (10, 20, 50, 100):
            per_page = 10
        q = manage_records_base_query(args)
        paginated = q.paginate(page=page, per_page=per_page, error_out=False)
        items = [serialize_recording_row(r) for r in paginated.items]
        return jsonify(
            {
                'items': items,
                'total': paginated.total,
                'per_page': per_page,
                'current_page': page,
                'total_pages': paginated.pages,
            }
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/manage/users-options')
@login_required
def manage_users_options():
    if not _can_access_manage_data():
        return jsonify({'error': '无权访问数据管理'}), 403
    users = User.query.order_by(User.username.asc()).all()
    return jsonify({'items': [{'id': u.id, 'username': u.username, 'role': u.role} for u in users]})


def _invalidate_recording_core(recording_id):
    if current_user.role not in ('admin', 'super_admin', 'inspector'):
        return jsonify({'error': '仅管理员或质检审核人员可标记无效数据'}), 403
    recording = Recording.query.get_or_404(recording_id)
    if recording.status == 'invalid':
        return jsonify({'success': True, 'message': '该记录已是无效状态'})
    recording.status = 'invalid'
    recording.invalidated_at = datetime.utcnow()
    recording.invalidated_by = current_user.id
    db.session.commit()
    return jsonify({'success': True, 'message': '已逻辑删除（标记无效），物理文件保留'})


@app.route('/api/manage/invalidate/<int:recording_id>', methods=['POST'])
@login_required
def invalidate_recording(recording_id):
    return _invalidate_recording_core(recording_id)


@app.route('/api/manage/recording/<int:recording_id>', methods=['DELETE'])
@login_required
def delete_recording(recording_id):
    """兼容旧接口：改为逻辑删除，禁止物理删文件。"""
    return _invalidate_recording_core(recording_id)


# ---------- 反馈#6：垃圾箱（软删除保留 30 天，可恢复，到期物理删除）----------
TRASH_RETENTION_DAYS = 30


def _release_claim_lock(recording_id):
    """移入垃圾箱时释放处理领取锁，避免文件被锁死无人能处理。"""
    try:
        model_redis.delete(f'security:process_claim:{recording_id}')
    except Exception:
        app.logger.warning('释放领取锁失败 recording=%s', recording_id, exc_info=True)


def _hard_purge_recording(recording):
    """物理删除：媒体/字幕文件 + 处理结果/审核日志/数据集项 + 记录行。"""
    for p in (recording.file_path, recording.subtitle_srt_path, recording.subtitle_json_path):
        if p and os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                app.logger.warning('物理删除文件失败: %s', p, exc_info=True)
    ProcessingResult.query.filter_by(recording_id=recording.id).delete(synchronize_session=False)
    AuditLog.query.filter_by(recording_id=recording.id).delete(synchronize_session=False)
    DataSetItem.query.filter_by(recording_id=recording.id).delete(synchronize_session=False)
    db.session.delete(recording)


def _purge_expired_trash():
    """清理垃圾箱中超过保留期的记录（启动时 + 查看垃圾箱时懒触发）。"""
    cutoff = datetime.utcnow() - timedelta(days=TRASH_RETENTION_DAYS)
    rows = (
        Recording.query.filter(Recording.deleted_at.isnot(None))
        .filter(Recording.deleted_at < cutoff)
        .all()
    )
    for r in rows:
        app.logger.info('垃圾箱过期物理删除 recording=%s deleted_at=%s', r.id, r.deleted_at)
        _hard_purge_recording(r)
    if rows:
        db.session.commit()
    return len(rows)


def _parse_id_list(data):
    ids = []
    for x in (data.get('ids') or []):
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue
    return ids


@app.route('/api/manage/trash', methods=['POST'])
@app.route('/api/manage/trash/<int:recording_id>', methods=['POST'])
@login_required
def trash_recording(recording_id=None):
    """移入垃圾箱（单条 /api/manage/trash/<id>，批量 body: {ids:[...]}）。"""
    if not _can_access_manage_data():
        return jsonify({'error': '无权访问数据管理'}), 403
    data = request.get_json(silent=True) or {}
    ids = [recording_id] if recording_id else _parse_id_list(data)
    ids = [i for i in ids if i]
    if not ids:
        return jsonify({'error': '请提供有效的 ids 数组'}), 400
    n = 0
    for rid in ids:
        recording = Recording.query.get(rid)
        if not recording or recording.deleted_at is not None:
            continue
        recording.deleted_at = datetime.utcnow()
        recording.deleted_by = current_user.id
        _release_claim_lock(rid)
        n += 1
    db.session.commit()
    return jsonify({'success': True, 'count': n,
                    'message': f'已将 {n} 个文件移入垃圾箱（保留 {TRASH_RETENTION_DAYS} 天，可恢复）'})


@app.route('/api/manage/restore', methods=['POST'])
@app.route('/api/manage/restore/<int:recording_id>', methods=['POST'])
@login_required
def restore_recording(recording_id=None):
    """从垃圾箱恢复（单条 /api/manage/restore/<id>，批量 body: {ids:[...]}）。"""
    if not _can_access_manage_data():
        return jsonify({'error': '无权访问数据管理'}), 403
    data = request.get_json(silent=True) or {}
    ids = [recording_id] if recording_id else _parse_id_list(data)
    ids = [i for i in ids if i]
    if not ids:
        return jsonify({'error': '请提供有效的 ids 数组'}), 400
    n = 0
    for rid in ids:
        recording = Recording.query.get(rid)
        if not recording or recording.deleted_at is None:
            continue
        recording.deleted_at = None
        recording.deleted_by = None
        n += 1
    db.session.commit()
    return jsonify({'success': True, 'count': n, 'message': f'已恢复 {n} 个文件'})


@app.route('/api/manage/purge', methods=['POST'])
@app.route('/api/manage/purge/<int:recording_id>', methods=['POST'])
@login_required
def purge_recording(recording_id=None):
    """彻底删除（不可恢复）：仅管理员；单条 /api/manage/purge/<id>，批量 body: {ids:[...]}。"""
    if not _is_system_admin():
        return jsonify({'error': '仅管理员可彻底删除文件'}), 403
    data = request.get_json(silent=True) or {}
    ids = [recording_id] if recording_id else _parse_id_list(data)
    ids = [i for i in ids if i]
    if not ids:
        return jsonify({'error': '请提供有效的 ids 数组'}), 400
    n = 0
    for rid in ids:
        recording = Recording.query.get(rid)
        if not recording or recording.deleted_at is None:
            continue
        _hard_purge_recording(recording)
        n += 1
    db.session.commit()
    return jsonify({'success': True, 'count': n, 'message': f'已彻底删除 {n} 个文件（不可恢复）'})


@app.route('/api/manage/preview/<int:recording_id>')
@login_required
def preview_recording(recording_id):
    if not _can_access_manage_data():
        return jsonify({'error': '无权访问数据管理'}), 403
    recording = Recording.query.get_or_404(recording_id)
    ft = dm_detect_file_type(recording.filename)
    if ft == 'video':
        return jsonify({'type': 'video', 'url': f'/uploads/{recording.filename}'})
    if ft == 'audio':
        return jsonify({'type': 'audio', 'url': f'/uploads/{recording.filename}'})
    if ft == 'image':
        return jsonify({'type': 'image', 'url': f'/uploads/{recording.filename}'})
    return jsonify({'type': 'text', 'content': recording.text_content or '无文本内容'})


@app.route('/api/manage/preview-detail/<int:recording_id>')
@login_required
def preview_detail_recording(recording_id):
    if not _can_access_manage_data():
        return jsonify({'error': '无权访问数据管理'}), 403
    recording = Recording.query.get_or_404(recording_id)
    row = serialize_recording_row(recording)
    pr = _latest_processing_result(recording.id)
    timeline = []
    if pr and pr.timeline_json:
        try:
            timeline = json.loads(pr.timeline_json)
        except (json.JSONDecodeError, TypeError):
            timeline = []
    ft = dm_detect_file_type(recording.filename)
    media_url = f'/uploads/{recording.filename}' if os.path.isfile(recording.file_path) else None
    return jsonify(
        {
            **row,
            'timeline': timeline,
            'modality': ft,
            'media_url': media_url,
        }
    )


@app.route('/api/manage/download/<int:recording_id>')
@login_required
def download_recording(recording_id):
    if not _can_access_manage_data():
        return jsonify({'error': '无权访问数据管理'}), 403
    recording = Recording.query.get_or_404(recording_id)
    if os.path.exists(recording.file_path):
        return send_file(recording.file_path, as_attachment=True, download_name=recording.filename)
    return jsonify({'error': '文件不存在'}), 404


@app.route('/api/manage/batch-download', methods=['POST'])
@login_required
def batch_download_recordings():
    if not _can_access_manage_data():
        return jsonify({'error': '无权访问数据管理'}), 403
    data = request.get_json(silent=True) or {}
    ids = data.get('ids') or []
    clean = []
    for x in ids:
        try:
            clean.append(int(x))
        except (TypeError, ValueError):
            continue
    if not clean:
        return jsonify({'error': '请提供有效的 ids 数组'}), 400
    zpath, fname = create_batch_zip(clean)

    @after_this_request
    def _cleanup(resp):
        try:
            os.remove(zpath)
        except OSError:
            pass
        return resp

    return send_file(zpath, as_attachment=True, download_name=fname)


@app.route('/api/manage/export', methods=['POST'])
@login_required
def export_data():
    """兼容旧版导出；ZIP 媒体包请用批量下载接口。"""
    if not _can_access_manage_data():
        return jsonify({'error': '无权访问数据管理'}), 403
    try:
        body = request.get_json(silent=True) or {}
        data_type = body.get('dataType', 'all')
        export_format = body.get('format', 'json')
        if export_format == 'zip':
            return jsonify({'error': '请使用「批量下载」打包原始文件'}), 400
        args = {}
        if data_type == 'pending':
            args['statuses'] = 'pending'
        elif data_type == 'completed':
            args['statuses'] = 'completed'
        elif data_type == 'rejected':
            args['statuses'] = 'rejected'
        elif data_type == 'video':
            args['file_type'] = 'video'
        elif data_type == 'audio':
            args['file_type'] = 'audio'
        recs = manage_records_base_query(args).all()
        fields = ['id', 'filename', 'status', 'text_content', 'created_at', 'uploader_username']
        rows = build_export_rows(recs, fields)
        prefix = f'export_{data_type}_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}'
        # 表精简后：导出审计统一入 system_event_logs（event_type='export'）
        db.session.add(SystemEventLog(
            event_type='export',
            actor_id=current_user.id,
            success=True,
            action_type='legacy_export',
            summary=f'{export_format}:{data_type} rows={len(rows)}',
            detail_json=json.dumps(
                {'scope_type': 'legacy_export', 'scope_ref': data_type,
                 'format': export_format, 'fields': fields, 'row_count': len(rows)},
                ensure_ascii=False),
        ))
        db.session.commit()
        return export_as_response(rows, export_format, prefix)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/manage/export-v2', methods=['POST'])
@login_required
def export_manage_v2():
    if not _can_access_manage_data():
        return jsonify({'error': '无权访问数据管理'}), 403
    body = request.get_json(silent=True) or {}
    fmt = (body.get('format') or 'json').lower()
    if fmt not in ('json', 'csv'):
        return jsonify({'error': 'format 仅支持 json 或 csv'}), 400
    scope = (body.get('scope') or 'filter').lower()
    # f2 安全：导出字段白名单，file_path 等服务器路径仅 admin/super_admin 可导出
    _EXPORT_FIELD_WHITELIST = {
        'id', 'filename', 'status', 'text_content', 'created_at', 'updated_at',
        'uploader_username', 'uploader_id', 'file_type', 'duration_seconds',
        'acquisition_audit_status', 'source_channel', 'remark', 'file_path',
    }
    raw_fields = body.get('fields') or ['filename', 'status', 'text_content', 'created_at']
    if not isinstance(raw_fields, list):
        return jsonify({'error': 'fields 必须为数组'}), 400
    fields = [f for f in raw_fields if isinstance(f, str) and f in _EXPORT_FIELD_WHITELIST]
    if not _is_system_admin():
        fields = [f for f in fields if f != 'file_path']
    if not fields:
        fields = ['filename', 'status', 'text_content', 'created_at']
    recs = []
    scope_ref = ''
    if scope == 'dataset':
        dsid = body.get('dataset_id')
        if not dsid:
            return jsonify({'error': 'dataset_id required'}), 400
        ds = DataSet.query.get_or_404(dsid)
        if not _is_system_admin() and ds.created_by != current_user.id:
            return jsonify({'error': 'Forbidden'}), 403
        scope_ref = str(dsid)
        id_list = [x.recording_id for x in DataSetItem.query.filter_by(dataset_id=dsid).all()]
        recs = Recording.query.filter(Recording.id.in_(id_list)).all() if id_list else []
    elif scope == 'ids':
        raw_ids = body.get('recording_ids') or []
        id_list = []
        for x in raw_ids:
            try:
                id_list.append(int(x))
            except (TypeError, ValueError):
                continue
        id_list = id_list[:10000]  # f2：限制单次导出规模
        scope_ref = f'ids:{len(id_list)}'
        recs = Recording.query.filter(Recording.id.in_(id_list)).all() if id_list else []
    else:
        fargs = normalize_manage_args(body.get('filters') or {})
        recs = manage_records_base_query(fargs).all()
        scope_ref = 'filter'
    rows = build_export_rows(recs, fields)
    # 表精简后：导出审计统一入 system_event_logs（event_type='export'）
    db.session.add(SystemEventLog(
        event_type='export',
        actor_id=current_user.id,
        success=True,
        action_type=f'export_{scope}',
        summary=f'{fmt}:{scope}:{scope_ref} rows={len(rows)}',
        detail_json=json.dumps(
            {'scope_type': scope, 'scope_ref': scope_ref,
             'format': fmt, 'fields': fields, 'row_count': len(rows)},
            ensure_ascii=False),
    ))
    db.session.commit()
    prefix = f'export_{scope}_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}'
    return export_as_response(rows, fmt, prefix)


@app.route('/api/manage/filter-templates', methods=['GET', 'POST'])
@login_required
def filter_templates_api():
    if request.method == 'GET':
        rows = (
            FilterTemplate.query.filter_by(user_id=current_user.id)
            .order_by(FilterTemplate.created_at.desc())
            .all()
        )
        return jsonify(
            {
                'items': [
                    {'id': r.id, 'name': r.name, 'criteria': json.loads(r.criteria_json)}
                    for r in rows
                ]
            }
        )
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    criteria = data.get('criteria')
    if not name or criteria is None:
        return jsonify({'error': 'name 与 criteria 必填'}), 400
    row = FilterTemplate(
        user_id=current_user.id,
        name=name,
        criteria_json=json.dumps(criteria, ensure_ascii=False),
    )
    db.session.add(row)
    db.session.commit()
    return jsonify({'success': True, 'id': row.id})


@app.route('/api/manage/filter-templates/<int:tid>', methods=['DELETE'])
@login_required
def filter_templates_delete(tid):
    row = FilterTemplate.query.get_or_404(tid)
    if row.user_id != current_user.id and not _is_system_admin():
        return jsonify({'error': 'Forbidden'}), 403
    db.session.delete(row)
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/manage/datasets', methods=['GET', 'POST'])
@login_required
def datasets_list_create():
    if request.method == 'GET':
        q = DataSet.query
        if not _is_system_admin():
            q = q.filter_by(created_by=current_user.id)
        rows = q.order_by(DataSet.created_at.desc()).all()
        out = []
        for d in rows:
            n = DataSetItem.query.filter_by(dataset_id=d.id).count()
            out.append(
                {
                    'id': d.id,
                    'name': d.name,
                    'description': d.description,
                    'version_label': d.version_label,
                    # 反馈#15：输出格式 / 导出内容 / 划分
                    'output_format': getattr(d, 'output_format', None) or 'jsonl',
                    'include_fields': getattr(d, 'include_fields', None) or 'media,transcript,metadata',
                    'split_rule': getattr(d, 'split_rule', None) or 'none',
                    'item_count': n,
                    'created_at': d.created_at.isoformat() if d.created_at else None,
                }
            )
        return jsonify({'items': out})
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400
    ver = (data.get('version') or data.get('version_label') or 'V1.0')[:32]
    # 反馈#15：输出格式白名单 + 导出内容白名单 + 划分规则白名单
    output_format = (data.get('output_format') or 'jsonl').strip()
    if output_format not in ('jsonl', 'json', 'csv', 'zip_media'):
        output_format = 'jsonl'
    raw_fields = data.get('include_fields') or ''
    if isinstance(raw_fields, (list, tuple)):
        fields_list = [str(x).strip() for x in raw_fields]
    else:
        fields_list = [x.strip() for x in str(raw_fields).split(',')]
    allowed_fields = {'media', 'transcript', 'timeline', 'metadata', 'audit'}
    fields_list = [f for f in fields_list if f in allowed_fields]
    if not fields_list:
        fields_list = ['media', 'transcript', 'metadata']
    split_rule = (data.get('split_rule') or 'none').strip()
    if split_rule not in ('none', '8:1:1', '7:1:2', '9:0.5:0.5'):
        split_rule = 'none'
    ds = DataSet(
        name=name[:255],
        description=(data.get('description', '') or '')[:2000],
        version_label=ver,
        output_format=output_format,
        include_fields=','.join(fields_list),
        split_rule=split_rule,
        created_by=current_user.id,
    )
    db.session.add(ds)
    db.session.commit()
    return jsonify({'success': True, 'id': ds.id,
                    'output_format': output_format,
                    'include_fields': fields_list,
                    'split_rule': split_rule})


@app.route('/api/manage/datasets/<int:dsid>', methods=['DELETE'])
@login_required
def datasets_delete(dsid):
    ds = DataSet.query.get_or_404(dsid)
    if not _is_system_admin() and ds.created_by != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403
    DataSetItem.query.filter_by(dataset_id=dsid).delete()
    db.session.delete(ds)
    db.session.commit()
    return jsonify({'success': True})


def _dataset_meta_dict(ds, item_count=None):
    """反馈#15：数据集元信息（供条目查看与导出共用）。"""
    if item_count is None:
        item_count = DataSetItem.query.filter_by(dataset_id=ds.id).count()
    return {
        'id': ds.id,
        'name': ds.name,
        'description': ds.description or '',
        'version_label': ds.version_label or 'V1.0',
        'output_format': getattr(ds, 'output_format', None) or 'jsonl',
        'include_fields': getattr(ds, 'include_fields', None) or 'media,transcript,metadata',
        'split_rule': getattr(ds, 'split_rule', None) or 'none',
        'item_count': item_count,
        'created_at': ds.created_at.isoformat() if ds.created_at else None,
    }


def _dataset_split_bucket(recording_id, split_rule):
    """反馈#15：按 recording id 确定性划分 train/val/test（同一条目永远落在同一子集）。"""
    if not split_rule or split_rule == 'none':
        return None
    if split_rule == '8:1:1':
        m = recording_id % 10
        return 'train' if m < 8 else ('val' if m == 8 else 'test')
    if split_rule == '7:1:2':
        m = recording_id % 10
        return 'train' if m < 7 else ('val' if m == 7 else 'test')
    if split_rule == '9:0.5:0.5':
        m = recording_id % 20
        return 'train' if m < 18 else ('val' if m == 18 else 'test')
    return None


def _dataset_build_records(ds):
    """取出数据集全部有效条目，返回 (rows, fields)；rows 为 serialize_recording_row 结果并带 split。"""
    fields = [f for f in (getattr(ds, 'include_fields', '') or '').split(',') if f] \
        or ['media', 'transcript', 'metadata']
    items = DataSetItem.query.filter_by(dataset_id=ds.id).order_by(DataSetItem.added_at.asc()).all()
    rows = []
    for it in items:
        rec = Recording.query.get(it.recording_id)
        if not rec or rec.status == 'invalid' or getattr(rec, 'deleted_at', None):
            continue
        row = serialize_recording_row(rec)
        row['split'] = _dataset_split_bucket(rec.id, getattr(ds, 'split_rule', 'none')) or 'all'
        rows.append(row)
    return rows, fields


def _dataset_sample(row, fields):
    """反馈#15：按勾选的导出内容组装单条样本。"""
    sample = {'id': row['id']}
    if 'metadata' in fields:
        sample.update({
            'filename': row['filename'],
            'media_type': row['file_type'],
            'status': row['status'],
            'task_no': row['task_no'],
            'md5': row['file_md5'],
            'duration': row['duration'],
            'file_size': row['file_size'],
            'uploader': row['uploader_username'],
            'processor': row['processor_username'],
            'auditor': row['auditor_username'],
            'created_at': row['created_at'],
            'processed_at': row['last_processed_at'],
            'reviewed_at': row['last_audit_at'],
        })
    if 'transcript' in fields:
        sample['transcript'] = row.get('text_full') or ''
    if 'timeline' in fields:
        sample['timeline'] = row.get('timeline_preview') or []
    if 'audit' in fields:
        sample['audit'] = {
            'auditor': row['auditor_username'],
            'reviewed_at': row['last_audit_at'],
            'remark': row.get('review_remark') or '',
        }
    if 'media' in fields:
        sample['media_file'] = row['filename']
    sample['split'] = row.get('split') or 'all'
    return sample


@app.route('/api/manage/datasets/<int:dsid>/items', methods=['GET', 'POST', 'DELETE'])
@login_required
def datasets_items(dsid):
    ds = DataSet.query.get_or_404(dsid)
    if not _is_system_admin() and ds.created_by != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403
    # 反馈#15：GET 查看条目（分页，默认前 100 条）
    if request.method == 'GET':
        try:
            page_size = max(1, min(int(request.args.get('page_size', 100)), 500))
        except (TypeError, ValueError):
            page_size = 100
        q = DataSetItem.query.filter_by(dataset_id=dsid).order_by(DataSetItem.added_at.desc())
        total = q.count()
        items = q.limit(page_size).all()
        out = []
        for it in items:
            rec = Recording.query.get(it.recording_id)
            if not rec:
                continue
            row = serialize_recording_row(rec)
            out.append({
                'id': row['id'],
                'filename': row['filename'],
                'media_type': row['file_type'],
                'status': row['status'],
                'uploader_username': row['uploader_username'],
                'processor_username': row['processor_username'],
                'added_at': it.added_at.isoformat() if it.added_at else None,
            })
        return jsonify({'dataset': _dataset_meta_dict(ds, total), 'total': total, 'items': out})
    body = request.get_json(silent=True) or {}
    ids = body.get('recording_ids') or []
    if request.method == 'POST':
        for rid in ids:
            rec = Recording.query.get(rid)
            # 反馈#6：无效数据 / 垃圾箱中的文件不允许加入数据集
            if not rec or rec.status == 'invalid' or rec.deleted_at is not None:
                continue
            if not DataSetItem.query.filter_by(dataset_id=dsid, recording_id=rid).first():
                db.session.add(DataSetItem(dataset_id=dsid, recording_id=rid))
        ds.updated_at = datetime.utcnow()
        db.session.commit()
        return jsonify({'success': True})
    for rid in ids:
        DataSetItem.query.filter_by(dataset_id=dsid, recording_id=rid).delete()
    ds.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/manage/datasets/<int:dsid>/export', methods=['GET'])
@login_required
def datasets_export(dsid):
    """反馈#15：按数据集配置导出。
    - jsonl：每行一条样本（划分时打包为 ZIP，含 train/val/test.jsonl）
    - json ：manifest 清单（含 dataset 元信息与全部样本，划分时分组）
    - csv ：表格（划分时打包为 ZIP）
    - zip_media：媒体文件 + manifest.json 打包（媒体按 train/val/test 分目录）
    """
    import csv as _csv
    import io as _io
    import zipfile as _zipfile

    if not _can_access_manage_data():
        return jsonify({'error': '无权导出数据集'}), 403
    ds = DataSet.query.get_or_404(dsid)
    if not _is_system_admin() and ds.created_by != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403

    rows, fields = _dataset_build_records(ds)
    if not rows:
        return jsonify({'error': '数据集为空，请先勾选数据并「加入数据集」'}), 400
    samples = [_dataset_sample(r, fields) for r in rows]
    fmt = getattr(ds, 'output_format', None) or 'jsonl'
    split_rule = getattr(ds, 'split_rule', None) or 'none'
    has_split = split_rule != 'none'
    groups = ('train', 'val', 'test') if has_split else ('all',)
    by_group = {g: [s for s in samples if s.get('split') == g] for g in groups}
    base_name = f"dataset_{ds.id}_{_safe_filename(ds.name)}_{ds.version_label or 'V1'}"
    meta = _dataset_meta_dict(ds, len(rows))
    counts = {g: len(by_group[g]) for g in groups}

    def _jsonl_text(ss):
        return '\n'.join(json.dumps(s, ensure_ascii=False) for s in ss)

    def _csv_text(ss):
        buf = _io.StringIO()
        cols = ['id', 'split', 'filename', 'media_type', 'status', 'task_no', 'md5',
                'duration', 'file_size', 'uploader', 'processor', 'auditor',
                'created_at', 'processed_at', 'reviewed_at', 'transcript', 'media_file']
        w = _csv.DictWriter(buf, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        for s in ss:
            w.writerow(s)
        return buf.getvalue()

    def _manifest_obj():
        return {'dataset': meta, 'split_rule': split_rule, 'counts': counts,
                'include_fields': fields, 'samples': samples}

    # JSON 清单：始终单文件（划分时样本内带 split 字段）
    if fmt == 'json':
        data = json.dumps(_manifest_obj(), ensure_ascii=False, indent=2).encode('utf-8')
        return send_file(_io.BytesIO(data), mimetype='application/json',
                         as_attachment=True, download_name=base_name + '.json')

    # JSONL / CSV：不划分 → 单文件；划分 → ZIP 分包
    if fmt in ('jsonl', 'csv'):
        if not has_split:
            text = _jsonl_text(samples) if fmt == 'jsonl' else _csv_text(samples)
            ext = 'jsonl' if fmt == 'jsonl' else 'csv'
            return send_file(_io.BytesIO(text.encode('utf-8-sig' if fmt == 'csv' else 'utf-8')),
                             mimetype='text/plain', as_attachment=True,
                             download_name=f'{base_name}.{ext}')
        buf = _io.BytesIO()
        with _zipfile.ZipFile(buf, 'w', _zipfile.ZIP_DEFLATED) as zf:
            for g in groups:
                ext = 'jsonl' if fmt == 'jsonl' else 'csv'
                text = _jsonl_text(by_group[g]) if fmt == 'jsonl' else _csv_text(by_group[g])
                enc = 'utf-8-sig' if fmt == 'csv' else 'utf-8'
                zf.writestr(f'{g}.{ext}', text.encode(enc))
            zf.writestr('manifest.json',
                        json.dumps(_manifest_obj(), ensure_ascii=False, indent=2).encode('utf-8'))
        buf.seek(0)
        return send_file(buf, mimetype='application/zip', as_attachment=True,
                         download_name=base_name + '.zip')

    # zip_media：媒体文件 + manifest 打包（媒体按划分目录组织）
    if fmt == 'zip_media':
        include_media = 'media' in fields
        total_bytes = sum(int(r.get('file_size') or 0) for r in rows)
        if include_media and total_bytes > 512 * 1024 * 1024:
            return jsonify({'error': f'媒体总大小 {total_bytes / 1024 / 1024:.0f}MB 超过 512MB 导出上限，'
                                     '请缩小数据集范围，或改用 JSONL/CSV 仅导出标注'}), 400
        buf = _io.BytesIO()
        used_names = set()
        missing = []
        with _zipfile.ZipFile(buf, 'w', _zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('manifest.json',
                        json.dumps(_manifest_obj(), ensure_ascii=False, indent=2).encode('utf-8'))
            if not include_media:
                zf.writestr('README.txt',
                            '该数据集导出未包含媒体文件（导出内容未勾选「媒体文件」）；样本标注见 manifest.json。'.encode('utf-8'))
            for r in rows:
                if not include_media:
                    break
                # serialize_recording_row 不含 file_path，回查 Recording 取服务器路径
                rec = Recording.query.get(r['id'])
                fpath = getattr(rec, 'file_path', None)
                fname = r['filename']
                if fname in used_names:
                    fname = f"{r['id']}_{fname}"
                used_names.add(fname)
                arc_dir = r.get('split') or 'all'
                arcname = f'media/{arc_dir}/{fname}' if has_split else f'media/{fname}'
                if fpath and os.path.exists(fpath):
                    try:
                        zf.write(fpath, arcname)
                    except OSError:
                        missing.append(fname)
                else:
                    missing.append(fname)
            if missing:
                zf.writestr('missing_files.txt',
                            ('以下媒体文件在服务器上不存在，未打入包内：\n' + '\n'.join(missing)).encode('utf-8'))
        buf.seek(0)
        return send_file(buf, mimetype='application/zip', as_attachment=True,
                         download_name=base_name + '_media.zip')

    return jsonify({'error': f'不支持的输出格式：{fmt}'}), 400


def _safe_filename(name):
    """文件名/数据集名清洗（导出下载名用）。"""
    import re as _re
    s = _re.sub(r'[\\/:*?"<>|\s]+', '_', str(name or 'dataset')).strip('_')
    return (s or 'dataset')[:60]


# API Endpoints for Crawl-based Data Collection
_CRAWL_SEARCH_RL_MAX = 10      # 每用户每分钟最多 10 次搜索
_CRAWL_SEARCH_RL_WINDOW = 60
_CRAWL_DOWNLOAD_RL_MAX = 5     # 每用户每小时最多 5 次下载提交
_CRAWL_DOWNLOAD_RL_WINDOW = 3600
_CRAWL_DOWNLOAD_LOCK_TTL = 1800  # 单次下载任务并发锁 30 分钟
_CRAWL_MAX_KEYWORDS = 5
_CRAWL_MAX_KEYWORD_LEN = 50
_CRAWL_MAX_DOWNLOAD_ITEMS = 30


@app.route('/api/crawl/search', methods=['POST'])
@login_required
def crawl_search():
    """
    互联网批量采集——搜索/提取候选条目（反馈#8：多站点 + 图片）。
    body: {
      "media_type": "video" | "image",         # 视频 / 图片
      "mode": "keyword" | "url",               # 关键字搜索 / 链接批量采集
      "site": "bilibili" | "youtube" | ...,    # 站点（视频模式）
      "keywords": "采访, 口述",                 # mode=keyword
      "page_url": "https://...",               # mode=url（合集/频道/网页）
      "count": 10|20|30, "duration_limit": 30   # duration_limit 仅 B站视频搜索（分钟）
    }
    f3：按用户限流 + 关键词数量/长度限制；异常不回传内部错误细节。
    """
    limited, wait = _fixed_window_rate_check(
        f'security:crawl_rl:search:{current_user.id}', _CRAWL_SEARCH_RL_MAX, _CRAWL_SEARCH_RL_WINDOW)
    if limited:
        return jsonify({'error': f'操作过于频繁，请 {wait} 秒后再试'}), 429

    try:
        data = request.get_json(silent=True) or {}
        media_type = str(data.get('media_type') or 'video').strip().lower()
        if media_type not in {'video', 'image'}:
            return jsonify({'error': 'media_type 仅支持 video / image'}), 400
        mode = str(data.get('mode') or 'keyword').strip().lower()
        if mode not in {'keyword', 'url'}:
            return jsonify({'error': 'mode 仅支持 keyword / url'}), 400
        site = str(data.get('site') or 'bilibili').strip().lower()
        page_url = str(data.get('page_url') or '').strip()
        try:
            count = int(data.get('count', 10))
            duration_limit = int(data.get('duration_limit', 30))
        except (TypeError, ValueError):
            return jsonify({'error': 'count / duration_limit 必须为整数'}), 400
        count = max(1, min(count, 30))
        if count not in {10, 20, 30}:
            count = 10
        duration_limit = max(1, min(duration_limit, 120))  # 时长上限（分钟）钳制
        # 反馈#15：忽略 robots 协议开关（默认遵守 robots.txt）
        ignore_robots = bool(data.get('ignore_robots'))

        if mode == 'url':
            if not page_url:
                return jsonify({'error': '链接采集模式需要 page_url'}), 400
            if media_type == 'image':
                results = web_crawler.extract_images_from_page(page_url, count, ignore_robots=ignore_robots)
            else:
                results = web_crawler.extract_videos_from_url(site, page_url, count, ignore_robots=ignore_robots)
            return jsonify({'success': True, 'count': len(results),
                            'items': results, 'media_type': media_type})

        # 关键字搜索模式
        keywords_raw = data.get('keywords', '')
        if not keywords_raw:
            return jsonify({'error': 'keywords is required'}), 400
        if isinstance(keywords_raw, str):
            keywords = [k.strip() for k in keywords_raw.split(',') if k.strip()]
        elif isinstance(keywords_raw, list):
            keywords = [str(k).strip() for k in keywords_raw if str(k).strip()]
        else:
            return jsonify({'error': 'keywords 必须为字符串或数组'}), 400
        if not keywords:
            return jsonify({'error': 'keywords is empty'}), 400
        if len(keywords) > _CRAWL_MAX_KEYWORDS:
            return jsonify({'error': f'单次最多 {_CRAWL_MAX_KEYWORDS} 个关键词'}), 400
        if any(len(k) > _CRAWL_MAX_KEYWORD_LEN for k in keywords):
            return jsonify({'error': f'单个关键词不能超过 {_CRAWL_MAX_KEYWORD_LEN} 字符'}), 400

        if media_type == 'image':
            results = web_crawler.search_images(keywords, count, ignore_robots=ignore_robots)
        else:
            # 视频：bilibili 走内置搜索 API（含播放量/弹幕/收藏），youtube 走 yt-dlp ytsearch
            results = web_crawler.search_videos(site, keywords, count, duration_limit,
                                                ignore_robots=ignore_robots)
        return jsonify({'success': True, 'count': len(results),
                        'items': results, 'media_type': media_type})
    except web_crawler.CrawlUnavailable as e:
        return jsonify({'error': str(e)}), 503
    except Exception:
        app.logger.exception('crawl_search 失败 user=%s', current_user.id)
        return jsonify({'error': '搜索失败，请稍后重试'}), 500


@app.route('/api/crawl/download', methods=['POST'])
@login_required
def crawl_download():
    """
    互联网批量采集——多选结果统一下载并入库（反馈#8：视频/图片 + meta 侧车）。
    body: {
      "media_type": "video" | "image",
      "items": [ ...search 返回的条目... ],
      "options": {"subtitles": true, "danmaku": true, "stats": true, "cover": true, "description": true},
      "remark": "本批视频/图片的内容备注（说明主要是干什么的）"
    }
    f3：按用户限流 + 单用户同时只允许一个下载任务（同步阻塞，Redis 锁防并发占满 worker）；
    单次最多 30 条；媒体统一落盘 UPLOAD_FOLDER（与人工上传一致，/uploads 可预览），
    每条写 .meta.json 侧车；MD5 去重；异常不回传内部错误细节。
    """
    limited, wait = _fixed_window_rate_check(
        f'security:crawl_rl:download:{current_user.id}', _CRAWL_DOWNLOAD_RL_MAX, _CRAWL_DOWNLOAD_RL_WINDOW)
    if limited:
        return jsonify({'error': f'下载提交过于频繁，请 {wait} 秒后再试'}), 429

    data = request.get_json(silent=True) or {}
    items = data.get('items', [])
    if not isinstance(items, list) or not items:
        return jsonify({'error': 'items is required'}), 400
    if len(items) > _CRAWL_MAX_DOWNLOAD_ITEMS:
        return jsonify({'error': f'单次最多下载 {_CRAWL_MAX_DOWNLOAD_ITEMS} 条'}), 400
    if not all(isinstance(it, dict) for it in items):
        return jsonify({'error': 'items 格式非法'}), 400
    media_type = str(data.get('media_type') or 'video').strip().lower()
    if media_type not in {'video', 'image'}:
        return jsonify({'error': 'media_type 仅支持 video / image'}), 400
    raw_options = data.get('options') or {}
    options = {
        'subtitles': bool(raw_options.get('subtitles')),
        'danmaku': bool(raw_options.get('danmaku')),
        'stats': bool(raw_options.get('stats')),
        'cover': bool(raw_options.get('cover')),
        'description': bool(raw_options.get('description')),
        # 反馈#15：下载阶段同样支持「忽略 robots 协议」（顶层或 options 内均可）
        'ignore_robots': bool(raw_options.get('ignore_robots') or data.get('ignore_robots')),
    }
    remark = str(data.get('remark') or '').strip()[:500]

    # 同步下载耗时较长（每视频间隔 + 实际下载），用 Redis 锁保证同一用户串行，
    # 避免多个长阻塞请求占满 waitress 工作线程
    lock_key = f'security:crawl:dl_lock:{current_user.id}'
    try:
        if not model_redis.set(lock_key, '1', nx=True, ex=_CRAWL_DOWNLOAD_LOCK_TTL):
            return jsonify({'error': '您有一个采集下载任务正在执行，请完成后再试'}), 409
    except Exception:
        app.logger.warning('crawl_download 锁写入失败，降级放行 user=%s', current_user.id, exc_info=True)

    created_ids = []
    dup_count = 0
    try:
        upload_dir = app.config['UPLOAD_FOLDER']
        os.makedirs(upload_dir, exist_ok=True)
        dl = web_crawler.download_all(
            items=items, media_type=media_type, options=options,
            remark=remark, dest_dir=upload_dir)

        for r in dl.get('results', []):
            if not r.get('ok'):
                continue
            local_path = r['path']
            # f2 安全纵深：入库文件必须位于 UPLOAD_FOLDER 内（文件名由服务端生成，拒绝任意路径）
            if not _is_path_within(upload_dir, local_path):
                app.logger.warning('crawl_download 拒绝白名单外路径: %s', local_path)
                continue
            # MD5 去重（入库前判定）：重复采集同一文件不重复入库，删除新文件节省存储
            file_md5 = compute_file_md5(local_path) if os.path.exists(local_path) else ''
            if file_md5 and Recording.query.filter_by(file_md5=file_md5).first():
                dup_count += 1
                try:
                    os.remove(local_path)
                    if r.get('meta_path') and os.path.exists(r['meta_path']):
                        os.remove(r['meta_path'])
                except OSError:
                    pass
                continue
            recording = Recording(
                filename=os.path.basename(local_path),
                file_path=os.path.abspath(local_path),
                recorded_by=current_user.id,
                uploader_id=current_user.id,
                status='pending',
                duration=(r.get('duration') or None) if media_type == 'video' else None,
                crawl_remark=remark or None,
                crawl_meta_json=json.dumps(r.get('meta') or {}, ensure_ascii=False),
                meta_json_path=r.get('meta_path'),
            )
            db.session.add(recording)
            db.session.flush()
            fill_acquisition_fields(recording, current_user.id, source_channel='crawl')
            # 逐条提交：长耗时批量下载中即使后续条目失败，已入库数据不丢
            db.session.commit()
            created_ids.append(recording.id)

        return jsonify({
            'success': True,
            'created_count': len(created_ids),
            'duplicate_count': dup_count,
            'failed': dl.get('failed', []),
            'recording_ids': created_ids,
        })
    except web_crawler.CrawlUnavailable as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 503
    except Exception:
        db.session.rollback()
        app.logger.exception('crawl_download 失败 user=%s', current_user.id)
        return jsonify({'error': '下载失败，请稍后重试'}), 500
    finally:
        try:
            model_redis.delete(lock_key)
        except Exception:
            app.logger.warning('crawl_download 锁释放失败 user=%s', current_user.id, exc_info=True)

def _parse_task_datetime(s):
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    s = str(s).strip()
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00').split('+')[0])
    except ValueError:
        try:
            return datetime.strptime(s[:10], '%Y-%m-%d')
        except ValueError:
            return None


def _assignment_recipients(task):
    rows = TaskAssignment.query.filter_by(task_id=task.id).all()
    return [r.user_id for r in rows]


def _task_per_item_points(task):
    """反馈#17：任务积分每件奖励——奖励总额按目标量均摊（每件至少 1）；未设奖励返回 0。"""
    reward = int(getattr(task, 'reward_task_points', 0) or 0)
    if reward <= 0:
        return 0
    req = max(1, task.required_count or 1)
    return max(1, int(round(reward / req)))


def _grant_task_compute_points(task, user_id, quota, admin_id):
    """反馈#17：任务发布/申领时按个人配额给执行人发放算力点。
    幂等：assignment.compute_points_granted>0 不重复发；grant_credits 内部提交。"""
    alloc = int(getattr(task, 'alloc_compute_points', 0) or 0)
    if alloc <= 0 or not user_id or quota <= 0:
        return 0
    asg = TaskAssignment.query.filter_by(task_id=task.id, user_id=user_id).first()
    if asg and (asg.compute_points_granted or 0) > 0:
        return 0
    req = max(1, task.required_count or 1)
    amount = max(1, int(round(alloc * quota / req)))
    try:
        credit_service.grant_credits(
            admin_id or task.created_by or user_id, user_id, amount, 'permanent',
            f'任务 {task.task_no or task.id} 算力点分配（配额 {quota} 件）',
            model_redis,
        )
        if asg:
            asg.compute_points_granted = amount
            db.session.commit()
        return amount
    except Exception:
        app.logger.warning('任务算力点发放失败 task=%s user=%s', task.id, user_id, exc_info=True)
        return 0


# API Endpoints for Task Management（统计 + 兼容列表）
@app.route('/api/tasks/statistics')
@login_required
def get_task_statistics():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    rec_q = Recording.query
    if start_date:
        d0 = _parse_task_datetime(start_date)
        if d0:
            rec_q = rec_q.filter(Recording.created_at >= d0)
    if end_date:
        d1 = _parse_task_datetime(end_date)
        if d1:
            rec_q = rec_q.filter(Recording.created_at < d1 + timedelta(days=1))

    recordings = rec_q.all()

    # 反馈#8：数据类型统计覆盖视频/音频/图片（file_type 缺失时按文件名后缀推断）
    def _rec_modality(r):
        return (r.file_type or detect_modality(r.filename or '') or 'video')

    total_videos = sum(1 for r in recordings if _rec_modality(r) == 'video')
    total_audios = sum(1 for r in recordings if _rec_modality(r) == 'audio')
    total_images = sum(1 for r in recordings if _rec_modality(r) == 'image')
    total_duration = sum(
        float(r.duration or 0) for r in recordings if _rec_modality(r) in ('video', 'audio')
    )

    task_rows = Task.query.order_by(Task.created_at.desc()).limit(200).all()
    task_list = []
    for t in task_rows:
        s = serialize_task_summary(t)
        s['task_type'] = t.task_type
        s['start_date'] = t.start_date.isoformat() if t.start_date else None
        s['end_date'] = t.end_date.isoformat() if t.end_date else None
        s['status'] = effective_workflow_status(t)
        task_list.append(s)

    users = User.query.order_by(User.id.asc()).all()
    user_performance = []
    for u in users:
        urecs = Recording.query.filter_by(recorded_by=u.id).all()
        vrec = sum(1 for r in urecs if _rec_modality(r) == 'video')
        arec = sum(1 for r in urecs if _rec_modality(r) == 'audio')
        irec = sum(1 for r in urecs if _rec_modality(r) == 'image')
        dur_sum = sum(float(r.duration or 0) for r in urecs if _rec_modality(r) in ('video', 'audio'))
        done_assign = (
            TaskAssignment.query.join(Task, TaskAssignment.task_id == Task.id)
            .filter(
                TaskAssignment.user_id == u.id,
                or_(Task.workflow_status.in_(('completed', 'archived')), Task.status == 'completed'),
            )
            .count()
        )
        user_performance.append(
            {
                'username': u.username,
                'nickname': getattr(u, 'nickname', None) or '',
                'videos_recorded': vrec,
                'audios_recorded': arec,
                'images_recorded': irec,
                'total_duration': float(dur_sum),
                'tasks_completed': done_assign,
                'compensation_earned': False,
            }
        )

    total_tasks = Task.query.count()
    pending_tasks = Task.query.filter(
        or_(
            Task.workflow_status.is_(None),
            ~Task.workflow_status.in_(('completed', 'archived', 'terminated')),
        )
    ).count()
    completed_tasks = Task.query.filter(
        or_(Task.workflow_status.in_(('completed', 'archived')), Task.status == 'completed')
    ).count()

    return jsonify(
        {
            'total_videos': total_videos,
            'total_audios': total_audios,
            'total_images': total_images,
            'total_duration': total_duration,
            'tasks': task_list,
            'user_performance': user_performance,
            'tasks_meta': {'total': total_tasks, 'pending': pending_tasks, 'completed': completed_tasks},
        }
    )


@app.route('/api/tasks')
@login_required
def get_tasks():
    page = request.args.get('page', 1, type=int)
    per_page = min(50, request.args.get('per_page', 10, type=int) or 10)
    tasks = Task.query.order_by(Task.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    return jsonify(
        {
            'tasks': [serialize_task_summary(t) for t in tasks.items],
            'total': tasks.total,
            'pages': tasks.pages,
            'current_page': page,
        }
    )


@app.route('/api/tasks/eligible-users')
@login_required
def tasks_eligible_users():
    """反馈#12「任务发起」：按任务类别返回可指定的完成人（仅任务发起人=系统管理员可用）。"""
    if not can_create_task(current_user):
        return jsonify({'error': '仅系统管理员/超级管理员可发起任务'}), 403
    cat = (request.args.get('category') or 'process').strip().lower()
    if cat not in ('collect', 'process', 'audit'):
        return jsonify({'error': '任务类别仅支持 collect / process / audit'}), 400
    users = eligible_users_for_category(cat)
    return jsonify({
        'category': cat,
        'users': [
            {'id': u.id, 'username': u.username,
             'nickname': u.nickname or u.full_name or u.username,
             'role': u.role, 'department': u.department or ''}
            for u in users
        ],
    })


@app.route('/api/tasks/compute-stats')
@login_required
def tasks_compute_stats():
    """反馈#6：算力消耗统计。

    管理员（admin/super_admin）看全平台：累计消耗/发放、按后端、按人员、30 天趋势；
    其他角色（录入员/审核员等）只看本人：余额、本人消耗、按后端、30 天趋势、近期流水。
    """
    try:
        is_admin = _is_system_admin()
        days = 30
        now = datetime.utcnow()
        since0 = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)

        conds = []
        if not is_admin:
            conds.append(ComputeUsageLog.user_id == current_user.id)

        def _q():
            q = ComputeUsageLog.query
            for c in conds:
                q = q.filter(c)
            return q

        total_credits = int(
            _q().filter(ComputeUsageLog.status == 'success')
            .with_entities(func.coalesce(func.sum(ComputeUsageLog.cost_credits), 0))
            .scalar() or 0
        )
        total_calls = _q().filter(ComputeUsageLog.status == 'success').count()
        cache_hits = _q().filter(ComputeUsageLog.metric_type == 'cache_hit').count()
        failed_calls = _q().filter(ComputeUsageLog.status != 'success').count()

        be_rows = (
            db.session.query(
                ComputeUsageLog.backend,
                func.coalesce(func.sum(ComputeUsageLog.cost_credits), 0),
                func.count(ComputeUsageLog.id),
            )
            .filter(ComputeUsageLog.created_at >= since0, ComputeUsageLog.status == 'success', *conds)
            .group_by(ComputeUsageLog.backend)
            .all()
        )
        by_backend = [
            {'backend': b or 'unknown', 'credits': int(c), 'calls': int(n)}
            for b, c, n in be_rows
        ]

        tr_rows = (
            db.session.query(
                func.date(ComputeUsageLog.created_at),
                func.coalesce(func.sum(ComputeUsageLog.cost_credits), 0),
                func.count(ComputeUsageLog.id),
            )
            .filter(ComputeUsageLog.created_at >= since0, ComputeUsageLog.status == 'success', *conds)
            .group_by(func.date(ComputeUsageLog.created_at))
            .all()
        )
        tr_map = {str(d): (int(c), int(n)) for d, c, n in tr_rows}
        trend = []
        for i in range(days):
            d = (since0 + timedelta(days=i)).strftime('%Y-%m-%d')
            c, n = tr_map.get(d, (0, 0))
            trend.append({'date': d, 'credits': c, 'calls': n})

        payload = {
            'success': True,
            'scope': 'all' if is_admin else 'me',
            'total_credits': total_credits,
            'total_calls': total_calls,
            'cache_hits': cache_hits,
            'failed_calls': failed_calls,
            'by_backend': by_backend,
            'trend_days': days,
            'trend': trend,
        }

        if is_admin:
            user_rows = (
                db.session.query(
                    ComputeUsageLog.user_id,
                    func.coalesce(func.sum(ComputeUsageLog.cost_credits), 0),
                    func.count(ComputeUsageLog.id),
                )
                .filter(ComputeUsageLog.status == 'success')
                .group_by(ComputeUsageLog.user_id)
                .all()
            )
            by_user = []
            for uid, c, n in user_rows:
                u = User.query.get(uid)
                by_user.append({
                    'user_id': uid,
                    'username': u.username if u else str(uid),
                    'nickname': (getattr(u, 'nickname', None) or '') if u else '',
                    'role': u.role if u else '',
                    'credits': int(c),
                    'calls': int(n),
                })
            by_user.sort(key=lambda x: -x['credits'])
            granted_total = int(
                db.session.query(func.coalesce(func.sum(UserCreditGrant.credits), 0)).scalar() or 0
            )
            payload['by_user'] = by_user
            payload['granted_total'] = granted_total
        else:
            try:
                payload['balance'] = credit_service.get_balance(current_user.id, model_redis)
            except Exception:
                payload['balance'] = None
            recent = _q().order_by(ComputeUsageLog.created_at.desc()).limit(8).all()
            payload['recent'] = [
                {
                    'backend': r.backend,
                    'modality': r.modality,
                    'metric_type': r.metric_type,
                    'metric_value': r.metric_value,
                    'cost_credits': r.cost_credits,
                    'status': r.status,
                    'created_at': r.created_at.isoformat() if r.created_at else None,
                }
                for r in recent
            ]
        return jsonify(payload)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------- 任务工作流 API（发起 / 申领 / 执行 / 复核 / 评分 / 归档）----------
@app.route('/api/workflow/tasks', methods=['GET', 'POST'])
@login_required
def workflow_tasks():
    if request.method == 'GET':
        scope = request.args.get('scope', 'all')
        # 反馈#9 需求3：非管理员无权查看全站任务，scope=all 强制收敛为本人相关
        if scope == 'all' and not _is_system_admin():
            scope = 'mine'
        wf = request.args.get('workflow_status')
        cat = request.args.get('task_category')
        q = Task.query
        if scope == 'mine':
            ids = [a.task_id for a in TaskAssignment.query.filter_by(user_id=current_user.id).all()]
            q = q.filter(Task.id.in_(ids)) if ids else q.filter(Task.id == -1)
        elif scope == 'pool':
            q = q.filter_by(assign_mode='pool', workflow_status='pending_claim')
            if not _is_system_admin():
                allowed = [c for c, r in CATEGORY_ROLE.items() if r == current_user.role]
                if not allowed:
                    return jsonify({'error': '角色不可查看公海'}), 403
                q = q.filter(Task.task_category.in_(allowed))
        if wf:
            q = q.filter(Task.workflow_status == wf)
        if cat:
            q = q.filter(Task.task_category == cat)
        rows = q.order_by(Task.created_at.desc()).limit(300).all()
        return jsonify({'items': [serialize_task_summary(t, include_assignments=True) for t in rows]})

    if not can_create_task(current_user):
        return jsonify({'error': '仅系统管理员/超级管理员可创建并发布任务'}), 403
    body = request.get_json(silent=True) or {}
    task = Task(
        title=(body.get('title') or '未命名任务').strip()[:255],
        description=body.get('description') or '',
        acceptance_criteria=body.get('acceptance_criteria') or '',
        start_date=_parse_task_datetime(body.get('start_date')) or datetime.utcnow(),
        end_date=_parse_task_datetime(body.get('end_date')) or (datetime.utcnow() + timedelta(days=7)),
        required_count=max(1, int(body.get('required_count') or 1)),
        task_type=int(body.get('task_type') or 2),
        priority=(body.get('priority') or 'medium').lower(),
        task_category=(body.get('task_category') or 'process').lower(),
        task_subtype=(body.get('task_subtype') or '')[:40],
        assign_mode=(body.get('assign_mode') or 'manual').lower(),
        max_claim_per_user=max(1, int(body.get('max_claim_per_user') or 1)),
        # 反馈#17：双积分——任务积分奖励（完成挣得）与算力点分配（发布时发放给执行人）
        reward_task_points=max(0, int(body.get('reward_task_points') or 0)),
        alloc_compute_points=max(0, int(body.get('alloc_compute_points') or 0)),
        workflow_status='draft',
        created_by=current_user.id,
        status='pending',
    )
    task.task_no = generate_task_no('TASK')
    ok, err = validate_task_config(task)
    if not ok:
        return jsonify({'error': err}), 400
    db.session.add(task)
    db.session.flush()
    log_task_action(task, current_user.id, 'create', None, 'draft', {'title': task.title})
    db.session.commit()
    return jsonify({'success': True, 'id': task.id, 'task_no': task.task_no})


@app.route('/api/workflow/tasks/<int:task_id>', methods=['GET'])
@login_required
def workflow_task_detail(task_id):
    task = Task.query.get_or_404(task_id)
    if not _is_system_admin():
        if task.created_by != current_user.id:
            mine = TaskAssignment.query.filter_by(task_id=task.id, user_id=current_user.id).first()
            if not mine and effective_workflow_status(task) not in ('pending_claim',):
                return jsonify({'error': 'Forbidden'}), 403
    data = serialize_task_summary(task, include_assignments=True)
    logs = (
        TaskWorkflowLog.query.filter_by(task_id=task.id)
        .order_by(TaskWorkflowLog.created_at.desc())
        .limit(80)
        .all()
    )
    data['logs'] = [
        {
            'action': x.action_type,
            'from': x.from_status,
            'to': x.to_status,
            'user_id': x.user_id,
            'username': User.query.get(x.user_id).username if User.query.get(x.user_id) else '',
            'at': x.created_at.isoformat() if x.created_at else None,
            'detail': json.loads(x.detail_json) if x.detail_json else None,
        }
        for x in logs
    ]
    return jsonify(data)


@app.route('/api/workflow/tasks/<int:task_id>/publish', methods=['POST'])
@login_required
def workflow_task_publish(task_id):
    task = Task.query.get_or_404(task_id)
    if not is_task_creator_or_admin(current_user, task):
        return jsonify({'error': '无权限发布'}), 403
    if effective_workflow_status(task) != 'draft':
        return jsonify({'error': '当前状态不可发布'}), 400
    ok, err = validate_task_config(task)
    if not ok:
        return jsonify({'error': err}), 400
    body = request.get_json(silent=True) or {}
    prev = effective_workflow_status(task)
    task.published_at = datetime.utcnow()
    mode = task.assign_mode or 'manual'

    if mode == 'pool':
        task.workflow_status = 'pending_claim'
        users = eligible_users_for_category(task.task_category)
        notify_task_users(
            [u.id for u in users],
            current_user.id,
            task.id,
            f'【公海】新任务：{task.title}',
            f'任务编号 {task.task_no}，截止 {task.end_date}，请申领。',
            msg_type='task_published_pool',
            priority='high',
        )
    elif mode == 'manual':
        # f3：user_ids 去重（防重复分配导致配额拆分错误）+ 存在性校验
        raw_uids = [int(x) for x in (body.get('user_ids') or []) if str(x).isdigit()]
        uids = list(dict.fromkeys(raw_uids))
        if not uids:
            return jsonify({'error': '手动分配需提供 user_ids'}), 400
        valid_uids = []
        for uid in uids:
            u = User.query.get(uid)
            if u and u.is_active and not u.is_locked_account and (
                not u.account_valid_until or u.account_valid_until > datetime.utcnow()
            ):
                valid_uids.append(uid)
        if not valid_uids:
            return jsonify({'error': '所选用户均不可用（停用/注销/过期）'}), 400
        uids = valid_uids
        n = len(uids)
        base = task.required_count // n
        extra = task.required_count % n
        for i, uid in enumerate(uids):
            tq = base + (1 if i < extra else 0)
            if tq <= 0:
                continue
            db.session.add(
                TaskAssignment(
                    task_id=task.id,
                    user_id=uid,
                    target_quota=tq,
                    completed_count=0,
                    status='assigned',
                )
            )
        task.workflow_status = 'pending_execute'
        notify_task_users(
            uids,
            current_user.id,
            task.id,
            f'任务已分配：{task.title}',
            f'您被指派任务 {task.task_no}，请执行。',
            msg_type='task_assigned',
        )
    else:
        ok_au, msg = auto_assign(task, current_user.id)
        if not ok_au:
            return jsonify({'error': msg}), 400
        task.workflow_status = 'pending_execute'

    sync_legacy_status(task)
    log_task_action(task, current_user.id, 'publish', prev, task.workflow_status, {'assign_mode': mode})
    db.session.flush()
    # 反馈#17：发布时按个人配额给执行人发放任务算力点（公海模式在申领时发放）
    if mode in ('manual', 'auto'):
        try:
            for asg in TaskAssignment.query.filter_by(task_id=task.id).all():
                _grant_task_compute_points(task, asg.user_id, asg.target_quota or 1, current_user.id)
        except Exception:
            app.logger.warning('发布任务算力点发放异常 task=%s', task.id, exc_info=True)
    db.session.commit()
    return jsonify({'success': True, 'workflow_status': task.workflow_status})


@app.route('/api/workflow/tasks/<int:task_id>/claim', methods=['POST'])
@login_required
def workflow_task_claim(task_id):
    # f3：行锁串行化同一任务的并发申领，配合唯一约束防重复申领/超额领取
    task = Task.query.filter_by(id=task_id).with_for_update().first()
    if not task:
        return jsonify({'error': 'task not found'}), 404
    if task.assign_mode != 'pool' or effective_workflow_status(task) != 'pending_claim':
        return jsonify({'error': '非可申领状态'}), 400
    need_role = CATEGORY_ROLE.get(task.task_category, 'recorder')
    if current_user.role not in (need_role, 'admin', 'super_admin'):
        return jsonify({'error': '角色不可申领该任务'}), 403
    # 账号状态校验：停用/注销/过期账号不可申领
    cu = current_user
    if (not cu.is_active or cu.is_locked_account
            or (cu.account_valid_until and cu.account_valid_until < datetime.utcnow())):
        return jsonify({'error': '账号状态不可申领任务'}), 403
    if TaskAssignment.query.filter_by(task_id=task.id, user_id=current_user.id).first():
        return jsonify({'error': '已申领过'}), 400
    quota_sum = total_quota_for_task(task.id)
    remaining = task.required_count - quota_sum
    if remaining <= 0:
        return jsonify({'error': '任务已被领完'}), 400
    take = min(task.max_claim_per_user or 1, remaining)
    prev = task.workflow_status
    try:
        db.session.add(
            TaskAssignment(
                task_id=task.id,
                user_id=current_user.id,
                target_quota=take,
                completed_count=0,
                status='in_progress',
                claimed_from_pool=True,
            )
        )
        db.session.flush()
    except Exception:
        # 唯一约束兜底（并发下另一个请求已插入）
        db.session.rollback()
        return jsonify({'error': '已申领过或任务状态已变更，请刷新后重试'}), 400
    task.workflow_status = 'pending_execute'
    sync_legacy_status(task)
    log_task_action(task, current_user.id, 'claim', prev, task.workflow_status, {'quota': take})
    # 反馈#17：公海申领时按申领配额发放任务算力点
    db.session.flush()
    try:
        _grant_task_compute_points(task, current_user.id, take, task.created_by or current_user.id)
    except Exception:
        app.logger.warning('申领任务算力点发放异常 task=%s user=%s', task.id, current_user.id, exc_info=True)
    notify_task_users(
        [current_user.id],
        current_user.id,
        task.id,
        '',
        '',
        msg_type='task_claim_success',
        extra_variables={'quota': str(take)},
    )
    db.session.commit()
    return jsonify({'success': True, 'claimed_quota': take})


@app.route('/api/workflow/tasks/<int:task_id>/progress', methods=['POST'])
@login_required
def workflow_task_progress(task_id):
    task = Task.query.get_or_404(task_id)
    ws = effective_workflow_status(task)
    if ws not in ('pending_execute', 'pending_fix'):
        return jsonify({'error': '当前不可上报进度'}), 400
    asg = TaskAssignment.query.filter_by(task_id=task.id, user_id=current_user.id).first()
    if not asg:
        return jsonify({'error': '非任务执行人'}), 403
    body = request.get_json(silent=True) or {}
    delta = max(1, int(body.get('delta') or 1))
    cap = asg.target_quota or task.required_count
    prev_done = asg.completed_count
    asg.completed_count = min(cap, asg.completed_count + delta)
    asg.status = 'in_progress'
    actual_inc = asg.completed_count - prev_done  # 反馈#17：封顶后实际增量（任务积分按此计）
    log_task_action(
        task,
        current_user.id,
        'submit_progress',
        ws,
        ws,
        {'delta': delta, 'completed': asg.completed_count},
    )
    if prev_done < cap and asg.completed_count >= cap and task.created_by:
        notify_task_users(
            [current_user.id, task.created_by],
            current_user.id,
            task.id,
            '',
            '',
            msg_type='task_quota_completed',
        )
    db.session.commit()
    # 反馈#17：任务积分——完成上报即挣得（每件 per_item 点，追加流水，非消耗）
    try:
        per_item = _task_per_item_points(task)
        if per_item > 0 and actual_inc > 0:
            credit_service.earn_task_points(
                current_user.id, per_item * actual_inc,
                task_id=task.id, assignment_id=asg.id,
                reason=f'完成任务 {task.task_no or task.id} 上报 {actual_inc} 件',
            )
    except Exception:
        app.logger.warning('任务积分挣得记账失败 task=%s user=%s', task.id, current_user.id, exc_info=True)
    return jsonify({'success': True, 'completed_count': asg.completed_count, 'target_quota': cap})


@app.route('/api/workflow/tasks/<int:task_id>/submit-review', methods=['POST'])
@login_required
def workflow_task_submit_review(task_id):
    task = Task.query.get_or_404(task_id)
    ws = effective_workflow_status(task)
    if ws not in ('pending_execute', 'pending_fix'):
        return jsonify({'error': '状态不允许提交复核'}), 400
    asg = TaskAssignment.query.filter_by(task_id=task.id, user_id=current_user.id).first()
    if not asg and not _is_system_admin():
        return jsonify({'error': '仅执行人可提交复核'}), 403
    if total_completed_for_task(task.id) < task.required_count:
        return jsonify({'error': '完成量未达目标，无法提交复核'}), 400
    prev = ws
    task.workflow_status = 'pending_review'
    sync_legacy_status(task)
    inspectors = User.query.filter_by(role='inspector').all()
    notify_task_users(
        [u.id for u in inspectors],
        current_user.id,
        task.id,
        f'待复核：{task.title}',
        f'任务 {task.task_no} 已提交复核。',
        msg_type='task_submit_review',
        priority='high',
    )
    log_task_action(task, current_user.id, 'submit_review', prev, 'pending_review', {})
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/workflow/tasks/<int:task_id>/review', methods=['POST'])
@login_required
def workflow_task_review(task_id):
    if current_user.role not in ('inspector', 'admin', 'super_admin'):
        return jsonify({'error': '仅质检审核人员可复核'}), 403
    task = Task.query.get_or_404(task_id)
    if effective_workflow_status(task) != 'pending_review':
        return jsonify({'error': '非待复核状态'}), 400
    body = request.get_json(silent=True) or {}
    approve = bool(body.get('approve'))
    comments = (body.get('comments') or '').strip()
    prev = 'pending_review'
    if approve:
        task.workflow_status = 'pending_score'
        task.review_comments = comments or None
        notify_task_users(
            [task.created_by],
            current_user.id,
            task.id,
            f'复核通过：{task.title}',
            '请进行质量评分。',
            msg_type='task_review_pass',
            extra_variables={'detail': (comments or '请进行质量评分。')},
        )
        log_task_action(task, current_user.id, 'review_pass', prev, 'pending_score', {'comments': comments})
    else:
        if not comments:
            return jsonify({'error': '不通过需填写审核意见'}), 400
        task.workflow_status = 'pending_fix'
        task.review_comments = comments
        notify_task_users(
            _assignment_recipients(task),
            current_user.id,
            task.id,
            f'复核打回：{task.title}',
            comments,
            msg_type='task_review_reject',
            priority='urgent',
            extra_variables={'comments': comments},
        )
        log_task_action(task, current_user.id, 'review_reject', prev, 'pending_fix', {'comments': comments})
    sync_legacy_status(task)
    db.session.commit()
    return jsonify({'success': True, 'workflow_status': task.workflow_status})


@app.route('/api/workflow/tasks/<int:task_id>/score', methods=['POST'])
@login_required
def workflow_task_score(task_id):
    task = Task.query.get_or_404(task_id)
    if not is_task_creator_or_admin(current_user, task):
        return jsonify({'error': '仅发起人或管理员可评分'}), 403
    if effective_workflow_status(task) != 'pending_score':
        return jsonify({'error': '非待评分状态'}), 400
    body = request.get_json(silent=True) or {}
    score = float(body.get('score') or 0)
    if score < 0 or score > 100:
        return jsonify({'error': '分数建议 0–100'}), 400
    prev = task.workflow_status
    task.quality_score = score
    task.scored_by = current_user.id
    task.scored_at = datetime.utcnow()
    task.workflow_status = 'completed'
    sync_legacy_status(task)
    log_task_action(task, current_user.id, 'score', prev, 'completed', {'score': score})
    recipients = list(
        set(_assignment_recipients(task) + ([task.created_by] if task.created_by else []))
    )
    notify_task_users(
        recipients,
        current_user.id,
        task.id,
        f'任务质量评分完成：{task.title}',
        f'任务 {task.task_no} 已完成评分，可归档。',
        msg_type='task_scored',
        extra_variables={'score': str(score)},
    )
    admins = User.query.filter(User.role.in_(('admin', 'super_admin'))).all()
    admin_ids = [u.id for u in admins if u.id not in recipients]
    if admin_ids:
        notify_task_users(
            admin_ids,
            current_user.id,
            task.id,
            f'任务待归档：{task.title}',
            f'任务 {task.task_no} 已完成评分，可归档。',
            msg_type='task_pending_archive',
        )
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/workflow/tasks/<int:task_id>/archive', methods=['POST'])
@login_required
def workflow_task_archive(task_id):
    if not _is_system_admin():
        return jsonify({'error': '仅管理员可归档'}), 403
    task = Task.query.get_or_404(task_id)
    if effective_workflow_status(task) != 'completed':
        return jsonify({'error': '仅已完成任务可归档'}), 400
    prev = task.workflow_status
    task.workflow_status = 'archived'
    sync_legacy_status(task)
    log_task_action(task, current_user.id, 'archive', prev, 'archived', {})
    recips = list(set(_assignment_recipients(task) + ([task.created_by] if task.created_by else [])))
    if recips:
        notify_task_users(
            recips,
            current_user.id,
            task.id,
            f'任务已归档：{task.title}',
            '任务已最终归档。',
            msg_type='task_archived',
        )
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/workflow/tasks/<int:task_id>/pause', methods=['POST'])
@login_required
def workflow_task_pause(task_id):
    task = Task.query.get_or_404(task_id)
    if not is_task_creator_or_admin(current_user, task):
        return jsonify({'error': '无权限'}), 403
    body = request.get_json(silent=True) or {}
    reason = (body.get('reason') or '').strip()
    if not reason:
        return jsonify({'error': '请填写暂停原因'}), 400
    prev = effective_workflow_status(task)
    if prev == 'paused':
        return jsonify({'error': '任务已处于暂停状态，请勿重复操作'}), 400
    if prev not in ('pending_assign', 'pending_claim', 'pending_execute',
                    'pending_review', 'pending_fix', 'pending_score'):
        return jsonify({'error': '当前状态不可暂停'}), 400
    task.workflow_status = 'paused'
    task.pause_reason = reason
    task.paused_at = datetime.utcnow()
    sync_legacy_status(task)
    recipients = list(set([task.created_by] + _assignment_recipients(task)))
    notify_task_users(
        recipients,
        current_user.id,
        task.id,
        f'任务已暂停：{task.title}',
        reason,
        msg_type='task_paused',
        priority='high',
        extra_variables={'reason': reason},
    )
    log_task_action(task, current_user.id, 'pause', prev, 'paused', {'reason': reason})
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/workflow/tasks/<int:task_id>/resume', methods=['POST'])
@login_required
def workflow_task_resume(task_id):
    """f3：暂停任务恢复——仅 paused 可恢复；公海无人申领回到待申领，否则回到待执行。"""
    task = Task.query.get_or_404(task_id)
    if not is_task_creator_or_admin(current_user, task):
        return jsonify({'error': '无权限'}), 403
    prev = effective_workflow_status(task)
    if prev != 'paused':
        return jsonify({'error': '仅暂停状态的任务可恢复'}), 400
    if task.assign_mode == 'pool' and not TaskAssignment.query.filter_by(task_id=task.id).first():
        task.workflow_status = 'pending_claim'
    else:
        task.workflow_status = 'pending_execute'
    task.pause_reason = None
    task.paused_at = None
    sync_legacy_status(task)
    recipients = list(set([task.created_by] + _assignment_recipients(task)))
    notify_task_users(
        recipients,
        current_user.id,
        task.id,
        f'任务已恢复：{task.title}',
        '任务暂停已结束，可继续执行/申领。',
        msg_type='task_resumed',
        extra_variables={'detail': '任务已恢复执行。'},
    )
    log_task_action(task, current_user.id, 'resume', 'paused', task.workflow_status, {})
    db.session.commit()
    return jsonify({'success': True, 'workflow_status': task.workflow_status})


@app.route('/api/workflow/tasks/<int:task_id>/terminate', methods=['POST'])
@login_required
def workflow_task_terminate(task_id):
    task = Task.query.get_or_404(task_id)
    if not is_task_creator_or_admin(current_user, task):
        return jsonify({'error': '无权限'}), 403
    body = request.get_json(silent=True) or {}
    reason = (body.get('reason') or '').strip()
    if not reason:
        return jsonify({'error': '请填写终止原因'}), 400
    prev = effective_workflow_status(task)
    if prev == 'archived':
        return jsonify({'error': '已归档不可终止'}), 400
    task.workflow_status = 'terminated'
    task.terminate_reason = reason
    task.terminated_at = datetime.utcnow()
    sync_legacy_status(task)
    recipients = list(set([task.created_by] + _assignment_recipients(task)))
    notify_task_users(
        recipients,
        current_user.id,
        task.id,
        f'任务已终止：{task.title}',
        reason,
        msg_type='task_terminated',
        priority='high',
        extra_variables={'reason': reason},
    )
    log_task_action(task, current_user.id, 'terminate', prev, 'terminated', {'reason': reason})
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/workflow/tasks/<int:task_id>/return-request', methods=['POST'])
@login_required
def workflow_task_return_request(task_id):
    task = Task.query.get_or_404(task_id)
    if task.assign_mode != 'pool':
        return jsonify({'error': '仅公海任务可退领'}), 400
    asg = TaskAssignment.query.filter_by(task_id=task.id, user_id=current_user.id).first()
    if not asg or not asg.claimed_from_pool:
        return jsonify({'error': '无申领记录'}), 400
    # f3：终态/未开始状态不可退领；分配已完成不可退领
    ws = effective_workflow_status(task)
    if ws in ('draft', 'pending_claim', 'completed', 'archived', 'terminated'):
        return jsonify({'error': '当前任务状态不可退领'}), 400
    if asg.status not in ('assigned', 'in_progress'):
        return jsonify({'error': '当前分配状态不可退领'}), 400
    # f3：同一任务同一用户仅允许一条待审批退领申请，防重复提交
    dup = TaskReturnRequest.query.filter_by(
        task_id=task.id, user_id=current_user.id, status='pending'
    ).first()
    if dup:
        return jsonify({'error': '已有待审批的退领申请，请勿重复提交'}), 400
    body = request.get_json(silent=True) or {}
    reason = (body.get('reason') or '').strip()
    if not reason:
        return jsonify({'error': '请填写原因'}), 400
    db.session.add(
        TaskReturnRequest(task_id=task.id, user_id=current_user.id, reason=reason, status='pending')
    )
    notify_task_users(
        [task.created_by],
        current_user.id,
        task.id,
        f'退领申请：{task.title}',
        reason,
        msg_type='task_return_request',
        extra_variables={'reason': reason},
    )
    log_task_action(task, current_user.id, 'return_request', effective_workflow_status(task), None, {'reason': reason})
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/workflow/tasks/return-requests/<int:rid>/decide', methods=['POST'])
@login_required
def workflow_return_decide(rid):
    req = TaskReturnRequest.query.get_or_404(rid)
    task = Task.query.get_or_404(req.task_id)
    if not is_task_creator_or_admin(current_user, task):
        return jsonify({'error': '仅发起人/管理员可审批'}), 403
    if req.status != 'pending':
        return jsonify({'error': '已处理'}), 400
    body = request.get_json(silent=True) or {}
    approve = bool(body.get('approve'))
    req.status = 'approved' if approve else 'rejected'
    req.reviewed_by = current_user.id
    req.reviewed_at = datetime.utcnow()
    if approve:
        # f3：审批通过时复核任务未进入终态，避免终态后删除分配/回退状态
        ws_now = effective_workflow_status(task)
        if ws_now in ('completed', 'archived', 'terminated'):
            req.status = 'rejected'
            req.reviewed_by = current_user.id
            req.reviewed_at = datetime.utcnow()
            log_task_action(task, current_user.id, 'return_auto_reject', ws_now, ws_now,
                            {'request_id': rid, 'reason': '任务已终态，退领自动失效'})
            db.session.commit()
            return jsonify({'error': '任务已进入终态，退领申请自动失效'}), 400
        TaskAssignment.query.filter_by(task_id=task.id, user_id=req.user_id).delete()
        if TaskAssignment.query.filter_by(task_id=task.id).count() == 0 and task.assign_mode == 'pool':
            task.workflow_status = 'pending_claim'
            sync_legacy_status(task)
        notify_task_users(
            [req.user_id],
            current_user.id,
            task.id,
            '退领已通过',
            '任务已退回公海或解除您的申领。',
            msg_type='task_return_approved',
            extra_variables={'detail': '任务已退回公海或解除您的申领。'},
        )
    else:
        notify_task_users(
            [req.user_id],
            current_user.id,
            task.id,
            '退领未通过',
            '您的退领申请未获批准，请继续执行任务。',
            msg_type='task_return_rejected',
            extra_variables={'detail': '您的退领申请未获批准。'},
        )
    log_task_action(
        task,
        current_user.id,
        'return_approve' if approve else 'return_reject',
        effective_workflow_status(task),
        task.workflow_status,
        {'request_id': rid},
    )
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/workflow/tasks/<int:task_id>/logs/export')
@login_required
def workflow_task_logs_export(task_id):
    if not _is_system_admin():
        return jsonify({'error': '仅管理员可导出日志'}), 403
    task = Task.query.get_or_404(task_id)
    logs = TaskWorkflowLog.query.filter_by(task_id=task.id).order_by(TaskWorkflowLog.created_at.asc()).all()
    rows = []
    for x in logs:
        u = User.query.get(x.user_id)
        rows.append(
            {
                'time': x.created_at.isoformat() if x.created_at else '',
                'user': u.username if u else x.user_id,
                'action': x.action_type,
                'from_status': x.from_status,
                'to_status': x.to_status,
                'detail': x.detail_json,
            }
        )
    return Response(
        json.dumps(rows, ensure_ascii=False, indent=2),
        mimetype='application/json; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename=task_{task_id}_logs.json'},
    )


@app.route('/api/workflow/task-messages/my')
@login_required
def workflow_task_messages_my():
    """表精简后：任务类站内信统一走 inbox_messages（category='task'）。"""
    rows = (
        InboxMessage.query.filter(
            InboxMessage.recipient_id == current_user.id,
            InboxMessage.category == 'task',
            InboxMessage.user_deleted_at.is_(None),
        )
        .order_by(InboxMessage.created_at.desc())
        .limit(50)
        .all()
    )
    return jsonify(
        {
            'items': [
                {
                    'id': m.id,
                    'task_id': m.business_id if m.business_type == 'task' else None,
                    'title': m.title,
                    'body': m.body,
                    'created_at': m.created_at.isoformat() if m.created_at else None,
                }
                for m in rows
            ]
        }
    )

# API Endpoints for User Management（RBAC + 审计）
def _serialize_user_admin(u):
    return {
        'id': u.id,
        'username': u.username,
        'email': u.email,
        'phone': u.phone or '',
        'full_name': u.full_name or '',
        'nickname': getattr(u, 'nickname', None) or '',
        'admin_remark': getattr(u, 'admin_remark', None) or '',
        'department': u.department or '',
        'role': u.role,
        'active': u.is_active and not u.is_locked_account,
        'is_active': u.is_active,
        'is_locked_account': u.is_locked_account,
        'must_change_password': u.must_change_password,
        'account_valid_until': u.account_valid_until.isoformat() if u.account_valid_until else None,
        'created_at': u.created_at.isoformat() if u.created_at else None,
        'last_login_at': u.last_login_at.isoformat() if u.last_login_at else None,
    }


@app.route('/api/users')
@login_required
def get_users():
    if not _is_system_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    if request.args.get('list') == 'all':
        rows = User.query.order_by(User.id.asc()).all()
        return jsonify({'users': [_serialize_user_admin(u) for u in rows]})
    page = request.args.get('page', 1, type=int)
    per_page = min(100, request.args.get('per_page', 20, type=int) or 20)
    paginated = User.query.order_by(User.id.desc()).paginate(page=page, per_page=per_page, error_out=False)
    return jsonify(
        {
            'users': [_serialize_user_admin(u) for u in paginated.items],
            'total': paginated.total,
            'pages': paginated.pages,
            'current_page': page,
        }
    )


@app.route('/api/users/<int:user_id>', methods=['GET'])
@login_required
def get_user_one(user_id):
    if not _is_system_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    user = User.query.get_or_404(user_id)
    return jsonify(_serialize_user_admin(user))


@app.route('/api/users/<int:user_id>', methods=['PUT'])
@login_required
def update_user(user_id):
    if not _is_system_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    user = User.query.get_or_404(user_id)
    # f2：super_admin 账号仅 super_admin 可修改
    denied = _guard_super_admin_target(user)
    if denied is not None:
        return denied
    if user.is_locked_account:
        return jsonify({'error': '已注销账号不可修改'}), 400
    data = request.get_json(silent=True) or {}
    if 'username' in data and data['username']:
        if User.query.filter(User.username == data['username'], User.id != user.id).first():
            return jsonify({'error': '用户名已占用'}), 400
        user.username = data['username'].strip()[:80]
    if 'email' in data:
        user.email = (data['email'] or '').strip()[:120] or user.email
    if 'phone' in data:
        user.phone = (data['phone'] or '')[:32]
    if 'full_name' in data:
        user.full_name = (data['full_name'] or '')[:80]
    # 反馈#8：昵称（界面显示名）与管理员备注，均可空
    if 'nickname' in data:
        user.nickname = (data['nickname'] or '').strip()[:80] or None
    if 'admin_remark' in data:
        user.admin_remark = (data['admin_remark'] or '').strip()[:500] or None
    if 'department' in data:
        user.department = (data['department'] or '')[:128]
    if 'account_valid_until' in data:
        raw = data.get('account_valid_until')
        user.account_valid_until = _parse_task_datetime(raw) if raw else None
    # 反馈#14：最近登录时间允许超级管理员补录/更正（演示数据校对用）
    if 'last_login_at' in data and _is_super_admin():
        raw = data.get('last_login_at')
        user.last_login_at = _parse_task_datetime(raw) if raw else None
    if 'role' in data:
        new_role = data['role']
        if new_role == 'super_admin' and not _is_super_admin():
            return jsonify({'error': '仅超级管理员可分配该角色'}), 403
        if new_role not in ('super_admin', 'admin', 'recorder', 'inspector'):
            return jsonify({'error': '无效角色'}), 400
        if user.role != new_role:
            _log_user_audit(
                current_user.id,
                user.id,
                'role_change',
                {'from': user.role, 'to': new_role},
            )
            user.role = new_role
    if data.get('password'):
        ok, err = validate_password_strength(data['password'])
        if not ok:
            return jsonify({'error': err}), 400
        user.set_password(data['password'])
        user.must_change_password = bool(data.get('must_change_password', False))
        # f2：管理员重置密码后使既有会话失效，强制重新登录
        user.session_version = (user.session_version or 0) + 1
    db.session.commit()
    return jsonify({'success': True, 'message': '用户更新成功'})


@app.route('/api/users/<int:user_id>', methods=['DELETE'])
@login_required
def delete_user(user_id):
    """逻辑注销：永久锁定账号，保留数据。"""
    if not _is_system_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    if current_user.id == user_id:
        return jsonify({'error': '不可注销当前登录账号'}), 400
    user = User.query.get_or_404(user_id)
    denied = _guard_super_admin_target(user)
    if denied is not None:
        return denied
    user.is_locked_account = True
    user.is_active = False
    user.session_version = (user.session_version or 0) + 1
    _log_user_audit(current_user.id, user.id, 'account_deactivate', {'username': user.username})
    db.session.commit()
    _safe_emit_account(
        [user.id],
        'account_status',
        '您的账号已被管理员逻辑注销，将无法再登录系统。',
        username=user.username,
        sender_id=current_user.id,
    )
    return jsonify({'success': True, 'message': '账号已逻辑注销（锁定）'})


@app.route('/api/users/<int:user_id>/status', methods=['PUT'])
@login_required
def update_user_status_flags(user_id):
    if not _is_system_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    user = User.query.get_or_404(user_id)
    denied = _guard_super_admin_target(user)
    if denied is not None:
        return denied
    if user.is_locked_account:
        return jsonify({'error': '已注销账号不可变更启用状态'}), 400
    data = request.get_json(silent=True) or {}
    if 'active' in data:
        user.is_active = bool(data['active'])
        if not user.is_active:
            user.session_version = (user.session_version or 0) + 1
        _log_user_audit(
            current_user.id,
            user.id,
            'disable' if not user.is_active else 'enable',
            {},
        )
    db.session.commit()
    if 'active' in data:
        detail = '您的账号已被管理员停用，暂时无法登录。' if not user.is_active else '您的账号已重新启用。'
        _safe_emit_account(
            [user.id],
            'account_status',
            detail,
            username=user.username,
            sender_id=current_user.id,
        )
    return jsonify({'success': True})


def _issue_password_reveal(pwd):
    """f4：临时/初始密码不再随操作响应明文返回，改为 5 分钟一次性查看令牌。
    返回 reveal_token；管理员凭令牌调用 /api/users/reveal-temp-password 查看一次。"""
    token = secrets.token_urlsafe(24)
    key = f'security:pwd_reveal:{current_user.id}:{token}'
    model_redis.set(key, pwd, ex=300, nx=True)
    return token


@app.route('/api/users/reveal-temp-password', methods=['POST'])
@login_required
def reveal_temp_password():
    """一次性查看临时/初始密码（GET+DEL，5 分钟过期，仅生成者本人可查看）。"""
    if not _is_system_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json(silent=True) or {}
    token = (data.get('reveal_token') or '').strip()
    if not token:
        return jsonify({'error': 'reveal_token is required'}), 400
    key = f'security:pwd_reveal:{current_user.id}:{token}'
    try:
        pipe = model_redis.pipeline()
        pipe.get(key)
        pipe.delete(key)
        pwd, _ = pipe.execute()
    except Exception:
        app.logger.warning('reveal_temp_password Redis 异常', exc_info=True)
        return jsonify({'error': '服务暂不可用'}), 503
    if not pwd:
        return jsonify({'error': '令牌无效或已过期（密码仅可查看一次），请重新生成'}), 404
    return jsonify({'success': True, 'password': pwd,
                    'message': '请立即复制并通过安全渠道告知用户，关闭后无法再次查看'})


@app.route('/api/users/<int:user_id>/reset-password', methods=['POST'])
@login_required
def reset_user_password(user_id):
    if not _is_system_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    user = User.query.get_or_404(user_id)
    denied = _guard_super_admin_target(user)
    if denied is not None:
        return denied
    if user.is_locked_account or not user.is_active:
        return jsonify({'error': '账号不可用，无法重置密码'}), 400
    pwd = generate_initial_password()
    user.set_password(pwd)
    user.must_change_password = True
    user.failed_login_count = 0
    user.locked_until = None
    user.session_version = (user.session_version or 0) + 1
    _log_user_audit(current_user.id, user.id, 'password_reset_admin', {})
    db.session.commit()
    _safe_emit_account(
        [user.id],
        'account_password_reset',
        '',
        username=user.username,
        sender_id=current_user.id,
    )
    # f4：密码不明文进入本响应体，改发一次性查看令牌
    return jsonify({'success': True, 'reveal_token': _issue_password_reveal(pwd),
                    'message': '临时密码已生成，请点击"查看临时密码"（5分钟内仅可查看一次）'})


@app.route('/api/users/<int:user_id>/force-logout', methods=['POST'])
@login_required
def force_user_logout(user_id):
    if not _is_system_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    user = User.query.get_or_404(user_id)
    denied = _guard_super_admin_target(user)
    if denied is not None:
        return denied
    user.session_version = (user.session_version or 0) + 1
    _log_user_audit(current_user.id, user.id, 'force_logout', {})
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/users', methods=['POST'])
@login_required
def create_user():
    if not _is_system_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    if not username:
        return jsonify({'error': '用户名必填'}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({'error': '用户名已存在'}), 400
    role = data.get('role') or 'recorder'
    if role == 'super_admin' and not _is_super_admin():
        return jsonify({'error': '仅超级管理员可创建该角色'}), 403
    if role not in ('super_admin', 'admin', 'recorder', 'inspector'):
        return jsonify({'error': '无效角色'}), 400
    email = (data.get('email') or f'{username}@local.invalid')[:120]
    pwd = (data.get('password') or '').strip()
    must_change = True
    if pwd:
        ok, err = validate_password_strength(pwd)
        if not ok:
            return jsonify({'error': err}), 400
        must_change = bool(data.get('must_change_password', False))
    else:
        pwd = generate_initial_password()
        must_change = True
    new_user = User(
        username=username,
        email=email,
        role=role,
        full_name=(data.get('full_name') or '')[:80],
        nickname=((data.get('nickname') or '').strip()[:80] or None),
        department=(data.get('department') or '')[:128],
        phone=(data.get('phone') or '')[:32],
        admin_remark=((data.get('admin_remark') or '').strip()[:500] or None),
        is_active=True,
        is_locked_account=False,
        must_change_password=must_change,
    )
    av = data.get('account_valid_until')
    # 反馈#14：新建账号未指定有效期时默认 1 年，避免用户列表有效期空着
    new_user.account_valid_until = _parse_task_datetime(av) if av else (datetime.utcnow() + timedelta(days=365))
    new_user.set_password(pwd)
    db.session.add(new_user)
    db.session.flush()
    # 反馈#13：默认昵称由用户名派生（如 zhou.yating -> yating），重名追加序号；不再用「用户<ID>」
    if not (new_user.nickname or '').strip():
        new_user.nickname = _unique_derived_nickname(new_user.username)
    _log_user_audit(current_user.id, new_user.id, 'create_user', {'username': username, 'role': role})
    db.session.commit()
    _safe_emit_account(
        [new_user.id],
        'account_created',
        '',
        username=new_user.username,
        sender_id=current_user.id,
    )
    out = {'success': True, 'user_id': new_user.id, 'must_change_password': must_change}
    # f4：自动生成初始密码时不随本响应明文返回，改发一次性查看令牌（管理员显式设置密码则无需告知）
    if (data.get('return_initial_password') or not (data.get('password') or '').strip()) and not data.get('password'):
        out['reveal_token'] = _issue_password_reveal(pwd)
    return jsonify(out)


@app.route('/api/profile/me', methods=['GET', 'PUT'])
@login_required
def profile_me():
    if request.method == 'GET':
        u = current_user
        return jsonify(
            {
                'id': u.id,
                'username': u.username,
                'email': u.email,
                'phone': u.phone or '',
                'full_name': u.full_name or '',
                'nickname': getattr(u, 'nickname', None) or '',
                'department': u.department or '',
                'role': u.role,
                'account_valid_until': u.account_valid_until.isoformat() if u.account_valid_until else None,
            }
        )
    data = request.get_json(silent=True) or {}
    if 'email' in data:
        current_user.email = (data['email'] or '').strip()[:120]
    if 'phone' in data:
        current_user.phone = (data['phone'] or '')[:32]
    if 'full_name' in data:
        current_user.full_name = (data['full_name'] or '')[:80]
    # 反馈#8：用户可自助设置昵称（界面显示名）
    if 'nickname' in data:
        current_user.nickname = (data['nickname'] or '').strip()[:80] or None
    db.session.commit()
    _log_user_audit(current_user.id, current_user.id, 'profile_self_update', {})
    return jsonify({'success': True})


@app.route('/api/profile/change-password', methods=['POST'])
@login_required
def profile_change_password():
    data = request.get_json(silent=True) or {}
    old_p = data.get('old_password') or ''
    new_p = data.get('new_password') or ''
    if not current_user.check_password(old_p):
        return jsonify({'error': '原密码错误'}), 400
    ok, err = validate_password_strength(new_p)
    if not ok:
        return jsonify({'error': err}), 400
    current_user.set_password(new_p)
    current_user.must_change_password = False
    current_user.session_version = (current_user.session_version or 0) + 1
    _log_user_audit(current_user.id, current_user.id, 'password_change_self', {})
    db.session.commit()
    session['_sv'] = current_user.session_version
    return jsonify({'success': True})


@app.route('/api/profile/my-login-logs', methods=['GET'])
@login_required
def profile_my_login_logs():
    """当前用户查看本人登录/登出记录（非管理员接口）。
    表精简后：统一查 system_event_logs（event_type='login'）。"""
    rows = (
        SystemEventLog.query.filter(
            SystemEventLog.event_type == 'login',
            SystemEventLog.actor_id == current_user.id,
        )
        .order_by(SystemEventLog.created_at.desc())
        .limit(100)
        .all()
    )
    return jsonify(
        {
            'items': [
                {
                    'event_type': x.action_type,
                    'success': x.success,
                    'ip': x.ip_address,
                    'at': x.created_at.isoformat() if x.created_at else None,
                    'message': x.summary,
                }
                for x in rows
            ]
        }
    )


@app.route('/api/profile/my-audit-trail', methods=['GET'])
@login_required
def profile_my_audit_trail():
    """与当前账号相关的权限/密码/资料类审计摘要（本人可见）。
    表精简后：统一查 system_event_logs（event_type='user_audit'）。"""
    q = SystemEventLog.query.filter(
        SystemEventLog.event_type == 'user_audit',
        or_(
            SystemEventLog.target_user_id == current_user.id,
            SystemEventLog.actor_id == current_user.id,
        ),
    )
    rows = q.order_by(SystemEventLog.created_at.desc()).limit(100).all()
    return jsonify(
        {
            'items': [
                {
                    'action': x.action_type,
                    'detail': json.loads(x.detail_json) if x.detail_json else None,
                    'at': x.created_at.isoformat() if x.created_at else None,
                }
                for x in rows
            ]
        }
    )


@app.route('/api/audit/login-logs')
@login_required
def audit_login_logs():
    """表精简后：登录日志统一查 system_event_logs（event_type='login'）。"""
    uid = request.args.get('user_id', type=int)
    if not _is_system_admin():
        uid = current_user.id
    q = SystemEventLog.query.filter(SystemEventLog.event_type == 'login')
    if uid:
        q = q.filter(SystemEventLog.actor_id == uid)
    rows = q.order_by(SystemEventLog.created_at.desc()).limit(500).all()
    items = []
    for x in rows:
        username = ''
        if x.detail_json:
            try:
                username = (json.loads(x.detail_json) or {}).get('username_attempted', '') or ''
            except (json.JSONDecodeError, TypeError):
                username = ''
        if not username and x.actor_id:
            u = User.query.get(x.actor_id)
            username = u.username if u else ''
        items.append(
            {
                'id': x.id,
                'user_id': x.actor_id,
                'username': username,
                'event_type': x.action_type,
                'success': x.success,
                'ip': x.ip_address,
                'at': x.created_at.isoformat() if x.created_at else None,
                'message': x.summary,
            }
        )
    return jsonify({'items': items})


@app.route('/api/audit/user-actions')
@login_required
def audit_user_actions():
    """表精简后：账号审计统一查 system_event_logs（event_type='user_audit'）。"""
    if not _is_system_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    rows = (
        SystemEventLog.query.filter(SystemEventLog.event_type == 'user_audit')
        .order_by(SystemEventLog.created_at.desc())
        .limit(500)
        .all()
    )
    return jsonify(
        {
            'items': [
                {
                    'id': x.id,
                    'actor_id': x.actor_id,
                    'target_user_id': x.target_user_id,
                    'action': x.action_type,
                    'detail': json.loads(x.detail_json) if x.detail_json else None,
                    'at': x.created_at.isoformat() if x.created_at else None,
                }
                for x in rows
            ]
        }
    )


@app.route('/api/audit/security-export')
@login_required
def audit_security_export():
    """表精简后：安全导出统一取自 system_event_logs。"""
    if not _is_system_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    logs = (
        SystemEventLog.query.filter(SystemEventLog.event_type == 'login')
        .order_by(SystemEventLog.created_at.desc())
        .limit(2000)
        .all()
    )
    audits = (
        SystemEventLog.query.filter(SystemEventLog.event_type == 'user_audit')
        .order_by(SystemEventLog.created_at.desc())
        .limit(2000)
        .all()
    )
    payload = {
        'login_logs': [
            {
                'user_id': x.actor_id,
                'username': x.summary,
                'event': x.action_type,
                'success': x.success,
                'ip': x.ip_address,
                'at': x.created_at.isoformat() if x.created_at else None,
            }
            for x in logs
        ],
        'user_audits': [
            {
                'actor': x.actor_id,
                'target': x.target_user_id,
                'action': x.action_type,
                'at': x.created_at.isoformat() if x.created_at else None,
            }
            for x in audits
        ],
    }
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        mimetype='application/json; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=security_audit.json'},
    )


@app.route('/api/admin/sessions/recent')
@login_required
def admin_recent_sessions():
    if not _is_system_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    since = datetime.utcnow() - timedelta(minutes=30)
    rows = User.query.filter(User.last_login_at >= since).order_by(User.last_login_at.desc()).all()
    return jsonify(
        {
            'items': [
                {
                    'user_id': u.id,
                    'username': u.username,
                    'last_login_at': u.last_login_at.isoformat() if u.last_login_at else None,
                    'last_ip': u.last_login_ip,
                }
                for u in rows
            ]
        }
    )


@app.route('/account/first-password', methods=['GET', 'POST'])
@login_required
def change_initial_password():
    if not current_user.must_change_password:
        return redirect(url_for('index'))
    if request.method == 'POST':
        p1 = request.form.get('password') or ''
        p2 = request.form.get('password2') or ''
        if p1 != p2:
            flash('两次输入不一致', 'danger')
            return redirect(url_for('change_initial_password'))
        ok, err = validate_password_strength(p1)
        if not ok:
            flash(err, 'danger')
            return redirect(url_for('change_initial_password'))
        current_user.set_password(p1)
        current_user.must_change_password = False
        current_user.session_version = (current_user.session_version or 0) + 1
        _log_user_audit(current_user.id, current_user.id, 'first_password_change', {})
        db.session.commit()
        session['_sv'] = current_user.session_version
        flash('密码已更新', 'success')
        return redirect(url_for('index'))
    lang = get_language()
    return render_template(f'account_first_password_{lang}.html')


# API Endpoints for Algorithm Module (BAGEL async queue + cache)
@app.route('/api/algorithm/bagel/submit', methods=['POST'])
@login_required
def submit_bagel_task():
    # 反馈#7：算法调用为采集与处理人员动作
    if not _is_processor():
        return jsonify({'error': '仅数据采集与处理人员可调用算法处理'}), 403
    if not bagel_queue.health():
        return jsonify({'error': 'Redis queue is unavailable'}), 503

    data = request.get_json(silent=True) or {}
    recording_id = data.get('recording_id')
    if not recording_id:
        return jsonify({'error': 'recording_id is required'}), 400

    recording = Recording.query.get(recording_id)
    if not recording:
        return jsonify({'error': 'recording not found'}), 404

    try:
        task_id, backend, reused = _submit_algorithm_task(recording, data)
    except FatalBackendError as e:
        # 后端未配置 / payload 非法等不可重试错误：直接 400，不入队
        return jsonify({'error': str(e), 'code': 'backend_misconfigured'}), 400
    except (QuotaExceeded, CreditInsufficient) as e:
        return jsonify({'error': str(e), 'code': 'quota_exceeded'}), 429
    return jsonify({'success': True, 'task_id': task_id,
                    'status': 'done' if reused else 'queued',
                    'backend': backend, 'reused': reused})


@app.route('/api/algorithm/bagel/status/<task_id>')
@login_required
def bagel_task_status(task_id):
    if not bagel_queue.health():
        return jsonify({'error': 'Redis cache is unavailable'}), 503
    task = bagel_queue.get_task(task_id)
    if not task:
        return jsonify({'error': 'task not found'}), 404
    # 任务完成/失败时在此触发积分结算（幂等），随后回读最新 HASH
    _settle_algorithm_billing(task)
    task = bagel_queue.get_task(task_id) or task
    return jsonify({'success': True, 'task': task})


@app.route('/model-files/<path:filename>')
def model_file_download(filename):
    """
    外部模型 worker（autoDL/局域网）凭共享Token下载云端上传文件。
    无需登录会话；f4 起优先使用 Authorization: Bearer <MODEL_FILE_TOKEN> 请求头
    （避免 Token 出现在 URL/访问日志中）；?token= 仅保留给 BAGEL_FILE_TRANSFER=url
    模式下由 GPU 推理服务直接回拉的场景。未配置 Token 时禁用。
    """
    expected = Config.MODEL_FILE_TOKEN
    auth = request.headers.get('Authorization', '')
    token = auth[7:].strip() if auth.startswith('Bearer ') else ''
    if not token:
        token = request.args.get('token') or ''
    if not expected or not hmac.compare_digest(token, expected):
        return jsonify({'error': 'unauthorized'}), 403
    # send_from_directory 内置 safe_join，防止路径穿越；中文文件名可正常处理
    return send_from_directory(Config.UPLOAD_FOLDER, filename, as_attachment=True)


@app.route('/api/credits/me')
@login_required
def credits_me():
    """当前用户：积分余额、计价规则、近期算力流水。"""
    try:
        return jsonify({'success': True, **credit_service.my_credits(current_user.id, Config, model_redis)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/credits/overview')
@login_required
def admin_credits_overview():
    """管理员：全体用户积分余额 / 累计发放 / 累计消耗。"""
    if not _is_system_admin():
        return jsonify({'error': 'forbidden'}), 403
    try:
        return jsonify({
            'success': True,
            'enforced': credit_service.enforcement_active(Config),
            'enabled': bool(getattr(Config, 'CREDIT_ENABLED', True)),
            'pricing': {
                'deepseek_per_1k_tokens': Config.CREDIT_DEEPSEEK_PER_1K,
                'autodl_base': Config.CREDIT_AUTODL_BASE,
                'autodl_per_min': Config.CREDIT_AUTODL_PER_MIN,
                'local_base': Config.CREDIT_LOCAL_BASE,
                'local_per_min': Config.CREDIT_LOCAL_PER_MIN,
            },
            'users': credit_service.admin_overview(model_redis),
            # 反馈#17：分时段发放/消耗（近7日/近30日/本月/本年/全部）
            'range_stats': credit_service.credit_range_stats(),
            # 反馈#17：任务积分总览（累计挣得/已结算/未结算）
            'task_points_users': credit_service.task_points_overview(),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/task-points/settle', methods=['POST'])
@login_required
def admin_task_points_settle():
    """反馈#17：结算任务积分（登记 earned→settled，不扣减）。可按人结算。"""
    if not _is_system_admin():
        return jsonify({'error': 'forbidden'}), 403
    data = request.get_json(silent=True) or {}
    user_id = None
    try:
        user_id = int(data.get('user_id') or 0) or None
    except (TypeError, ValueError):
        user_id = None
    try:
        result = credit_service.settle_task_points(current_user.id, user_id=user_id)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    if user_id:
        _log_user_audit(current_user.id, user_id, 'task_points_settle', result)
    db.session.commit()
    return jsonify({'success': True, **result})


@app.route('/api/admin/credits/grant', methods=['POST'])
@login_required
def admin_credits_grant():
    """管理员给用户发放积分：{user_id, credits, period, reason}。"""
    if not _is_system_admin():
        return jsonify({'error': 'forbidden'}), 403
    data = request.get_json(silent=True) or {}
    try:
        user_id = int(data.get('user_id') or 0)
        credits = int(data.get('credits') or 0)
    except (TypeError, ValueError):
        return jsonify({'error': 'user_id/credits 必须为整数'}), 400
    period = (data.get('period') or 'permanent').strip().lower()
    # 反馈#17：周期标签白名单 7日/30日/1年/自定义/永久（旧标签 202609 等按永久兼容）
    if period not in ('7d', '30d', '1y', 'custom', 'permanent'):
        period = 'permanent'
    reason = (data.get('reason') or '管理员分配算力点').strip()
    custom_expire = None
    if period == 'custom':
        custom_expire = _parse_task_datetime(data.get('expire_at') or data.get('expire_date'))
        if not custom_expire:
            return jsonify({'error': '自定义周期需提供失效日期（expire_at, YYYY-MM-DD）'}), 400
        if custom_expire <= datetime.utcnow():
            return jsonify({'error': '失效日期必须晚于当前时间'}), 400
    try:
        row = credit_service.grant_credits(
            current_user.id, user_id, credits, period, reason, model_redis,
            credit_type=credit_service.CREDIT_TYPE_COMPUTE,
            custom_expire=custom_expire,
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    _log_user_audit(current_user.id, user_id, 'credit_grant',
                    {'credits': credits, 'period': period, 'reason': reason,
                     'expire_at': row.expire_at.isoformat() if row.expire_at else None})
    db.session.commit()
    return jsonify({'success': True, 'grant_id': row.id,
                    'expire_at': row.expire_at.isoformat() if row.expire_at else None,
                    'balance': credit_service.get_balance(user_id, model_redis)})


@app.route('/api/admin/model/quota')
@login_required
def admin_model_quota():
    """管理员查看模型后端状态、DeepSeek 配额、GPU 健康、Worker、死信数。"""
    if not _is_system_admin():
        return jsonify({'error': 'forbidden'}), 403
    quota = deepseek_quota_status(model_redis, Config)

    def _gpu_info(target):
        url = (getattr(Config, f'BAGEL_{target.upper()}_SERVICE_URL', '') or Config.BAGEL_SERVICE_URL)
        enabled = bool(getattr(Config, f'BAGEL_{target.upper()}_ENABLED', True))
        healthy, detail = (False, '未探活')
        if not enabled:
            healthy, detail = False, '已停用（管理员配置中未启用）'
        elif url:
            try:
                healthy, detail = gpu_backend_health(target, Config, model_redis)
            except Exception as e:
                detail = f'探活异常: {e}'
        return {'label': 'BAGEL满血版(autoDL)' if target == 'autodl' else 'BAGEL优化版(局域网)',
                'url': url, 'configured': bool(url), 'enabled': enabled,
                'healthy': healthy, 'health_detail': detail}

    try:
        workers = bagel_queue.list_workers()
    except Exception:
        workers = []
    try:
        dead_count = len(bagel_queue.list_dead(limit=200))
    except Exception:
        dead_count = 0
    return jsonify({
        'success': True,
        'default_backend': Config.MODEL_DEFAULT_BACKEND,
        'default_gpu_backend': getattr(Config, 'MODEL_DEFAULT_GPU_BACKEND', 'local'),
        'gpu_backends': {'autodl': _gpu_info('autodl'), 'local': _gpu_info('local')},
        'workers': workers,
        'dead_task_count': dead_count,
        'bagel_file_transfer': Config.BAGEL_FILE_TRANSFER,
        'deepseek_configured': bool(Config.DEEPSEEK_API_KEY),
        'deepseek_model': Config.DEEPSEEK_MODEL,
        'quota': quota,
    })


# ---------------- 反馈#7：DeepSeek 接口配置（超管专属"系统核心配置"）与流量统计 ----------------

@app.route('/api/admin/deepseek/config', methods=['GET'])
@login_required
def admin_deepseek_config_get():
    """超级管理员查看 DeepSeek 接口配置（Key 掩码返回）。系统管理员不可见。"""
    if not _is_super_admin():
        return jsonify({'error': '仅超级管理员可管理 DeepSeek 接口配置（系统核心配置）'}), 403
    try:
        quota = deepseek_quota_status(model_redis, Config)
    except Exception:
        quota = {'daily_used': 0, 'daily_limit': Config.DEEPSEEK_DAILY_TOKEN_LIMIT,
                 'monthly_used': 0, 'monthly_limit': Config.DEEPSEEK_MONTHLY_TOKEN_LIMIT}
    return jsonify({
        'success': True,
        'configured': bool(Config.DEEPSEEK_API_KEY),
        'api_key_masked': _mask_secret(Config.DEEPSEEK_API_KEY),
        'base_url': Config.DEEPSEEK_BASE_URL,
        'model': Config.DEEPSEEK_MODEL,
        'daily_token_limit': Config.DEEPSEEK_DAILY_TOKEN_LIMIT,
        'monthly_token_limit': Config.DEEPSEEK_MONTHLY_TOKEN_LIMIT,
        'quota': quota,
    })


@app.route('/api/admin/deepseek/config', methods=['PUT'])
@login_required
def admin_deepseek_config_put():
    """超级管理员更新 DeepSeek 接口配置：持久化到 system_settings 并即时写回 Config 运行时值。"""
    if not _is_super_admin():
        return jsonify({'error': '仅超级管理员可管理 DeepSeek 接口配置（系统核心配置）'}), 403
    data = request.get_json(silent=True) or {}
    changes = []

    if data.get('clear_api_key'):
        _set_setting('deepseek_api_key', '', remark='DeepSeek API Key（超管运行时管理）')
        Config.DEEPSEEK_API_KEY = ''
        changes.append('api_key_cleared')
    else:
        key = (data.get('api_key') or '').strip()
        if key:
            if not (key.startswith('sk-') or len(key) >= 24):
                return jsonify({'error': 'API Key 格式不正确（DeepSeek Key 通常以 sk- 开头）'}), 400
            _set_setting('deepseek_api_key', key, remark='DeepSeek API Key（超管运行时管理）')
            Config.DEEPSEEK_API_KEY = key
            changes.append('api_key')

    if 'base_url' in data:
        base = (data.get('base_url') or '').strip().rstrip('/')
        if base:
            if not base.startswith('http'):
                return jsonify({'error': 'Base URL 需以 http(s):// 开头'}), 400
            _set_setting('deepseek_base_url', base)
            Config.DEEPSEEK_BASE_URL = base
            changes.append('base_url')

    if 'model' in data:
        model = (data.get('model') or '').strip()
        if model:
            _set_setting('deepseek_model', model)
            Config.DEEPSEEK_MODEL = model
            changes.append('model')

    for field, attr in (('daily_token_limit', 'DEEPSEEK_DAILY_TOKEN_LIMIT'),
                        ('monthly_token_limit', 'DEEPSEEK_MONTHLY_TOKEN_LIMIT')):
        if field in data:
            try:
                iv = int(str(data.get(field) or '0').strip())
            except (TypeError, ValueError):
                return jsonify({'error': f'{field} 必须为非负整数（0 表示不限）'}), 400
            if iv < 0:
                return jsonify({'error': f'{field} 不能为负数'}), 400
            # 存储键与 _apply_deepseek_settings() 读取键保持一致（deepseek_ 前缀）
            _set_setting('deepseek_' + field, str(iv))
            setattr(Config, attr, iv)
            changes.append(field)

    try:
        _log_user_audit(current_user.id, current_user.id, 'deepseek_config_update',
                        {'changes': changes, 'base_url': Config.DEEPSEEK_BASE_URL,
                         'model': Config.DEEPSEEK_MODEL, 'configured': bool(Config.DEEPSEEK_API_KEY)})
    except Exception:
        pass
    db.session.commit()
    return jsonify({'success': True, 'message': 'DeepSeek 配置已保存并即时生效',
                    'changes': changes, 'configured': bool(Config.DEEPSEEK_API_KEY)})


# ---------------- 反馈#18：BAGEL GPU 后端（autoDL 满血版 / 局域网优化版）配置 ----------------

@app.route('/api/admin/bagel/config', methods=['GET'])
@login_required
def admin_bagel_config_get():
    """系统管理员查看 BAGEL 后端配置（autoDL / 局域网；Token 掩码返回）。"""
    if not _is_system_admin():
        return jsonify({'error': 'forbidden'}), 403
    target = (request.args.get('target') or '').strip().lower()
    if target not in _BAGEL_CONFIG_FIELDS:
        return jsonify({'error': 'target 必须为 autodl 或 local'}), 400
    snap = _bagel_config_snapshot(target)
    # 附带当前健康状态，便于配置弹窗展示
    snap['healthy'] = False
    snap['health_detail'] = '未探活'
    url = getattr(Config, f'BAGEL_{target.upper()}_SERVICE_URL', '') or ''
    if url and snap.get('enabled'):
        try:
            snap['healthy'], snap['health_detail'] = gpu_backend_health(target, Config, model_redis)
        except Exception as e:
            snap['health_detail'] = f'探活异常: {e}'
    elif not snap.get('enabled'):
        snap['health_detail'] = '已停用（配置中未启用）'
    return jsonify({'success': True, 'config': snap})


@app.route('/api/admin/bagel/config', methods=['PUT'])
@login_required
def admin_bagel_config_put():
    """系统管理员保存 BAGEL 后端配置：持久化到 system_settings 并即时写回 Config 运行时值。
    autoDL/局域网尚未开通时可先保存参数，启用开关关闭即不参与探活与路由。"""
    if not _is_system_admin():
        return jsonify({'error': 'forbidden'}), 403
    data = request.get_json(silent=True) or {}
    target = (data.get('target') or '').strip().lower()
    if target not in _BAGEL_CONFIG_FIELDS:
        return jsonify({'error': 'target 必须为 autodl 或 local'}), 400
    changes = []
    remark_map = {
        'autodl': 'autoDL 满血版 BAGEL 后端配置（反馈#18）',
        'local': '局域网优化版 BAGEL 后端配置（反馈#18）',
    }
    for suffix, attr, kind in _BAGEL_CONFIG_FIELDS[target]:
        if suffix not in data:
            continue
        raw = data.get(suffix)
        key = _bagel_setting_key(target, suffix)
        if kind == 'bool':
            val = '1' if bool(raw) else '0'
        else:
            val = str(raw or '').strip()
        if kind == 'url' and val:
            if not val.startswith('http'):
                return jsonify({'error': '服务地址需以 http(s):// 开头'}), 400
            val = val.rstrip('/')
        if kind == 'os' and val and val not in ('windows', 'linux'):
            return jsonify({'error': '操作系统需为 windows 或 linux'}), 400
        # service_token 留空表示不修改（掩码回填场景）；显式 clear_token=1 清空
        if suffix == 'service_token':
            if data.get('clear_token'):
                val = ''
            elif not val:
                continue
        _set_setting(key, val, remark=remark_map[target])
        # 即时写回运行时 Config
        if attr:
            if kind == 'bool':
                setattr(Config, attr, val == '1')
            elif kind == 'url':
                setattr(Config, attr, val)
            elif kind == 'os':
                setattr(Config, attr, val or 'linux')
            else:
                setattr(Config, attr, val)
        changes.append(suffix)
    try:
        _log_user_audit(current_user.id, current_user.id, 'bagel_config_update',
                        {'target': target, 'changes': changes})
    except Exception:
        pass
    db.session.commit()
    return jsonify({'success': True, 'message': 'BAGEL 后端配置已保存',
                    'changes': changes, 'config': _bagel_config_snapshot(target)})


@app.route('/api/admin/deepseek/usage')
@login_required
def admin_deepseek_usage():
    """DeepSeek 流量使用统计（系统管理员可见）：总调用/token/积分、近30天趋势、按人明细。
    数据源：compute_usage_logs（backend='deepseek'）。"""
    if not _is_system_admin():
        return jsonify({'error': 'forbidden'}), 403
    since = datetime.utcnow() - timedelta(days=30)
    base_q = ComputeUsageLog.query.filter_by(backend='deepseek')

    total_calls = base_q.filter_by(status='success').count()
    total_failed = base_q.filter_by(status='failed').count()
    total_tokens = db.session.query(func.coalesce(func.sum(ComputeUsageLog.metric_value), 0)).filter(
        ComputeUsageLog.backend == 'deepseek',
        ComputeUsageLog.status == 'success',
        ComputeUsageLog.metric_type == 'tokens',
    ).scalar() or 0
    total_credits = db.session.query(func.coalesce(func.sum(ComputeUsageLog.cost_credits), 0)).filter(
        ComputeUsageLog.backend == 'deepseek',
        ComputeUsageLog.status == 'success',
    ).scalar() or 0

    # 近 30 天趋势 + 按人明细（一次查询内存聚合，数据量小）
    rows = base_q.filter(ComputeUsageLog.created_at >= since).all()
    daily = {}
    per_user = {}
    for r in rows:
        d = r.created_at.strftime('%Y-%m-%d') if r.created_at else 'unknown'
        agg = daily.setdefault(d, {'tokens': 0, 'calls': 0, 'failed': 0, 'credits': 0})
        ua = per_user.setdefault(r.user_id, {'tokens': 0, 'calls': 0, 'failed': 0, 'credits': 0})
        if r.status == 'success':
            toks = float(r.metric_value or 0) if r.metric_type == 'tokens' else 0
            agg['tokens'] += toks
            agg['calls'] += 1
            agg['credits'] += int(r.cost_credits or 0)
            ua['tokens'] += toks
            ua['calls'] += 1
            ua['credits'] += int(r.cost_credits or 0)
        else:
            agg['failed'] += 1
            ua['failed'] += 1

    daily_series = [{'date': d, **daily[d]} for d in sorted(daily.keys())]
    user_rows = []
    for uid, agg in per_user.items():
        u = User.query.get(uid)
        user_rows.append({
            'user_id': uid,
            'username': u.username if u else str(uid),
            'nickname': (getattr(u, 'nickname', None) or '') if u else '',
            'role': u.role if u else '',
            'calls': agg['calls'],
            'failed': agg['failed'],
            'tokens': int(agg['tokens']),
            'credits': agg['credits'],
        })
    user_rows.sort(key=lambda x: x['tokens'], reverse=True)

    # 反馈#17：近24小时/近7日 token 费用累计 + 预计未来7日费用
    try:
        cost_stats = credit_service.llm_token_cost_stats(Config)
    except Exception:
        app.logger.warning('token 费用统计失败', exc_info=True)
        cost_stats = None

    return jsonify({
        'success': True,
        'totals': {
            'calls': total_calls,
            'failed': total_failed,
            'tokens': int(total_tokens),
            'credits': int(total_credits),
        },
        'daily': daily_series,
        'by_user': user_rows,
        'cost_stats': cost_stats,
        'model': Config.DEEPSEEK_MODEL,
        'configured': bool(Config.DEEPSEEK_API_KEY),
    })


@app.route('/api/admin/model/workers')
@login_required
def admin_model_workers():
    """管理员查看在线 Worker 与 GPU 后端健康。"""
    if not _is_system_admin():
        return jsonify({'error': 'forbidden'}), 403
    workers = bagel_queue.list_workers()
    gpu = {}
    for t in (BACKEND_AUTODL, BACKEND_LOCAL):
        url = (getattr(Config, f'BAGEL_{t.upper()}_SERVICE_URL', '') or Config.BAGEL_SERVICE_URL)
        healthy, detail = (False, '未配置')
        if url:
            try:
                healthy, detail = gpu_backend_health(t, Config, model_redis, force_refresh=bool(request.args.get('refresh')))
            except Exception as e:
                detail = f'探活异常: {e}'
        gpu[t] = {'url': url, 'configured': bool(url), 'healthy': healthy, 'detail': detail}
    return jsonify({'success': True, 'workers': workers, 'gpu_backends': gpu})


@app.route('/api/admin/model/dead-tasks')
@login_required
def admin_model_dead_tasks():
    """管理员查看死信队列（重试耗尽的任务）。"""
    if not _is_system_admin():
        return jsonify({'error': 'forbidden'}), 403
    return jsonify({'success': True, 'dead_tasks': bagel_queue.list_dead(limit=100)})


@app.route('/api/admin/model/dead-tasks/<task_id>/requeue', methods=['POST'])
@login_required
def admin_model_requeue_dead(task_id):
    """管理员把死信任务重新投回队列。"""
    if not _is_system_admin():
        return jsonify({'error': 'forbidden'}), 403
    ok = bagel_queue.requeue_dead(task_id)
    return jsonify({'success': ok})


@app.route('/api/admin/model/recover-stuck', methods=['POST'])
@login_required
def admin_model_recover_stuck():
    """
    卡死任务恢复：
    - queued 超时未消费（队列消息丢失/worker 全下线）→ 从 HASH 重建消息重投；
    - running 超时无心跳（worker 崩溃）→ 标记 failed 并退还冻结积分。
    """
    if not _is_system_admin():
        return jsonify({'error': 'forbidden'}), 403
    stuck = bagel_queue.scan_stuck(
        getattr(Config, 'BAGEL_STUCK_QUEUED_SECONDS', 600),
        getattr(Config, 'BAGEL_STUCK_RUNNING_SECONDS', 1800),
    )
    requeued, failed = [], []
    for tid in stuck.get('stuck_queued', []):
        if bagel_queue.requeue_task_by_id(tid):
            requeued.append(tid)
    for tid in stuck.get('stuck_running', []):
        task = bagel_queue.get_task(tid)
        if task:
            # CAS：仅当任务仍为 running 才判失败（worker 迟到回写 done/dead 不会与其互相覆盖）
            ok = bagel_queue.update_task_cas(tid, 'running', {
                'status': 'failed',
                'error': '恢复扫描：worker 心跳超时，判定失败',
            })
            if ok:
                _settle_algorithm_billing({**task, 'status': 'failed'})
                failed.append(tid)
    return jsonify({'success': True, 'requeued': requeued, 'marked_failed': failed})


@app.route('/api/admin/queue/live')
@login_required
def admin_queue_live():
    """反馈#8：队列实时监控——各状态任务数、在跑任务（所用模型/worker/已耗时）、排队与延迟重试明细。"""
    if not _is_system_admin():
        return jsonify({'error': 'forbidden'}), 403
    try:
        ov = bagel_queue.live_overview()
    except Exception as e:
        return jsonify({'error': f'queue scan failed: {e}'}), 500
    try:
        workers = bagel_queue.list_workers()
    except Exception:
        workers = []
    worker_map = {w.get('worker_id'): w for w in workers}

    # 提交人 ID -> 用户名
    uid_set = set()
    for bucket in ('running', 'queued', 'retry_wait'):
        for it in ov.get(bucket, []):
            try:
                uid = int(it.get('submitted_by') or 0)
                if uid:
                    uid_set.add(uid)
            except (TypeError, ValueError):
                pass
    user_map = {}
    if uid_set:
        try:
            for u in User.query.filter(User.id.in_(uid_set)).all():
                user_map[u.id] = (getattr(u, 'nickname', None) or u.username)
        except Exception:
            pass

    now_ts = time.time()

    def _enrich(it, queue_pos=None):
        ts = it.get('updated_at') or it.get('created_at')
        elapsed = None
        try:
            elapsed = max(0, int(now_ts - datetime.fromisoformat(ts).timestamp()))
        except (TypeError, ValueError):
            pass
        wait_left = None
        if it.get('next_run_at'):
            try:
                wait_left = max(0, int(float(it['next_run_at']) - now_ts))
            except (TypeError, ValueError):
                pass
        w = worker_map.get(it.get('worker_id')) or {}
        try:
            submitter = user_map.get(int(it.get('submitted_by') or 0), '')
        except (TypeError, ValueError):
            submitter = ''
        out = dict(it)
        out.update({
            'submitted_by_name': submitter,
            'worker_host': w.get('host', ''),
            'worker_pid': w.get('pid', ''),
            'elapsed_seconds': elapsed,
            'wait_left_seconds': wait_left,
            'queue_position': queue_pos,
        })
        return out

    running = [_enrich(it) for it in ov.get('running', [])]
    queued = [_enrich(it, queue_pos=i + 1) for i, it in enumerate(ov.get('queued', []))]
    retry_wait = [_enrich(it) for it in ov.get('retry_wait', [])]

    # 队列底层结构深度（HASH 过期后消息仍可能存在，用于交叉印证）
    depths = {}
    try:
        depths = {
            'queue_list': bagel_queue.client.llen(bagel_queue.queue_name),
            'delayed_zset': bagel_queue.client.zcard(bagel_queue.delayed_zset),
            'dead_list': bagel_queue.client.llen(bagel_queue.dead_queue_name),
        }
    except _redis_lib.RedisError:
        depths = {}

    return jsonify({
        'success': True,
        'counts': ov.get('counts', {}),
        'depths': depths,
        'running': running,
        'queued': queued,
        'retry_wait': retry_wait,
        'workers': workers,
        'server_time': datetime.utcnow().isoformat() + 'Z',
    })

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        limited, wait_sec, rl_key = _login_rate_check()
        if limited:
            _log_login_event(None, username, False, 'login_rate_limited',
                             'IP 登录尝试超限，需等待 %s 秒' % wait_sec)
            flash('登录尝试过于频繁，请约 %d 分钟后再试' % max(1, wait_sec // 60), 'warning')
            return redirect(url_for('login'))
        user = User.query.filter_by(username=username).first()
        if not user:
            _log_login_event(None, username, False, 'login_fail', '用户不存在')
            flash('用户名或密码错误', 'danger')
            return redirect(url_for('login'))
        if user.is_locked_account or not user.is_active:
            # f3：对外统一提示防账号枚举，真实原因仅写审计日志
            _log_login_event(user.id, username, False, 'login_fail', '账号已禁用或已注销')
            flash('用户名或密码错误', 'danger')
            return redirect(url_for('login'))
        if user.account_valid_until and user.account_valid_until < datetime.utcnow():
            _log_login_event(user.id, username, False, 'login_fail', '账号已过期')
            flash('用户名或密码错误', 'danger')
            return redirect(url_for('login'))
        if user.locked_until and user.locked_until > datetime.utcnow():
            _log_login_event(user.id, username, False, 'login_fail', '账号已锁定')
            flash('登录尝试过多，请稍后再试', 'warning')
            return redirect(url_for('login'))
        # f3：锁定期已过则失败计数归零，给用户新的尝试窗口，避免一次错误就再次锁定
        if user.locked_until and user.locked_until <= datetime.utcnow():
            user.failed_login_count = 0
            user.locked_until = None
            db.session.commit()
        if not user.check_password(password):
            user.failed_login_count = (user.failed_login_count or 0) + 1
            locked_now = user.failed_login_count >= MAX_FAILED_ATTEMPTS
            if locked_now:
                user.locked_until = datetime.utcnow() + timedelta(minutes=LOCK_MINUTES)
            db.session.commit()
            _log_login_event(user.id, username, False, 'login_fail', '密码错误')
            if locked_now and user.locked_until:
                lu = user.locked_until.strftime('%Y-%m-%d %H:%M') + ' UTC'
                _safe_emit_account([user.id], 'account_locked', '', username=user.username, locked_until=lu)
            flash('用户名或密码错误', 'danger')
            return redirect(url_for('login'))
        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = datetime.utcnow()
        user.last_login_ip = _client_ip()
        db.session.commit()
        login_user(user, remember=False)
        session.permanent = True
        session['_sv'] = user.session_version or 0
        session['_idle_ts'] = time.time()
        _log_login_event(user.id, username, True, 'login_success', None)
        if rl_key:
            try:
                model_redis.delete(rl_key)
            except Exception:
                pass
        if user.must_change_password:
            recent_first = (
                InboxMessage.query.filter(
                    InboxMessage.recipient_id == user.id,
                    InboxMessage.msg_type == 'account_first_login',
                    InboxMessage.created_at >= datetime.utcnow() - timedelta(days=3),
                ).first()
            )
            if not recent_first:
                _safe_emit_account(
                    [user.id],
                    'account_first_login',
                    '请登录后立即修改初始密码。',
                    username=user.username,
                )
            return redirect(url_for('change_initial_password'))
        return redirect(url_for('index'))
    lang = get_language()
    return render_template(f'login_{lang}.html')


@app.route('/logout', methods=['POST'])
@login_required
def logout():
    # f4 安全：登出改为 POST + 会话 CSRF 令牌，防止 <img src=/logout> 等跨站强制登出
    token = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token') or ''
    if not _csrf_valid(token):
        return jsonify({'error': 'invalid csrf token'}), 400
    _log_login_event(current_user.id, current_user.username, True, 'logout', None)
    logout_user()
    session.clear()
    return redirect(url_for('login'))


@app.route('/profile')
@login_required
def profile_page():
    lang = get_language()
    return render_template(f'profile_{lang}.html')


# Initialize database and create initial user
with app.app_context():
    db.create_all()

    # 反馈#6/#8：db.create_all() 不会给已存在的表补列，这里幂等 ALTER 补齐
    try:
        from sqlalchemy import inspect as _sa_inspect, text as _sa_text
        _insp = _sa_inspect(db.engine)
        _need_cols_by_table = {
            'recordings': {
                'review_remark': 'ALTER TABLE recordings ADD COLUMN review_remark TEXT NULL',
                'deleted_at': 'ALTER TABLE recordings ADD COLUMN deleted_at DATETIME NULL',
                'deleted_by': 'ALTER TABLE recordings ADD COLUMN deleted_by INT NULL',
                # 反馈#8：互联网采集内容备注 / 采集元数据 JSON / meta 侧车文件路径
                'crawl_remark': 'ALTER TABLE recordings ADD COLUMN crawl_remark TEXT NULL',
                'crawl_meta_json': 'ALTER TABLE recordings ADD COLUMN crawl_meta_json TEXT NULL',
                'meta_json_path': 'ALTER TABLE recordings ADD COLUMN meta_json_path VARCHAR(512) NULL',
            },
            'users': {
                # 反馈#8：账号昵称（显示名，如超管 GR）与管理员备注
                'nickname': 'ALTER TABLE users ADD COLUMN nickname VARCHAR(80) NULL',
                'admin_remark': 'ALTER TABLE users ADD COLUMN admin_remark TEXT NULL',
            },
            'data_sets': {
                # 反馈#15：数据集输出格式 / 导出内容 / 训练集划分
                'output_format': "ALTER TABLE data_sets ADD COLUMN output_format VARCHAR(16) NULL DEFAULT 'jsonl'",
                'include_fields': "ALTER TABLE data_sets ADD COLUMN include_fields VARCHAR(255) NULL DEFAULT 'media,transcript,metadata'",
                'split_rule': "ALTER TABLE data_sets ADD COLUMN split_rule VARCHAR(16) NULL DEFAULT 'none'",
            },
            # 反馈#17：双积分体系（算力点 + 任务积分）
            'user_credit_grants': {
                'credit_type': "ALTER TABLE user_credit_grants ADD COLUMN credit_type VARCHAR(16) NULL DEFAULT 'compute'",
                'expire_at': 'ALTER TABLE user_credit_grants ADD COLUMN expire_at DATETIME NULL',
            },
            'tasks': {
                'reward_task_points': 'ALTER TABLE tasks ADD COLUMN reward_task_points INT NULL DEFAULT 0',
                'alloc_compute_points': 'ALTER TABLE tasks ADD COLUMN alloc_compute_points INT NULL DEFAULT 0',
            },
            'task_assignments': {
                'task_points_earned': 'ALTER TABLE task_assignments ADD COLUMN task_points_earned INT NULL DEFAULT 0',
                'compute_points_granted': 'ALTER TABLE task_assignments ADD COLUMN compute_points_granted INT NULL DEFAULT 0',
            },
        }
        with db.engine.begin() as _conn:
            for _tbl, _need_cols in _need_cols_by_table.items():
                try:
                    _existing = {c['name'] for c in _insp.get_columns(_tbl)}
                except Exception:
                    continue
                for _name, _ddl in _need_cols.items():
                    if _name not in _existing:
                        _conn.execute(_sa_text(_ddl))
                        app.logger.info('%s 补列完成: %s', _tbl, _name)
            _idx_names = {ix['name'] for ix in _insp.get_indexes('recordings')}
            _rec_cols = {c['name'] for c in _insp.get_columns('recordings')}
            if 'deleted_at' in _rec_cols and 'ix_recordings_deleted_at' not in _idx_names:
                try:
                    _conn.execute(_sa_text('CREATE INDEX ix_recordings_deleted_at ON recordings (deleted_at)'))
                except Exception:
                    pass  # 索引已存在或不支持时忽略
    except Exception:
        app.logger.warning('补列迁移失败（可手动执行迁移脚本）', exc_info=True)

    # 反馈#8：超级管理员的显示昵称设置为 GR（仅当未设置昵称时）
    # 反馈#9：超管登录账号已由 testuser 更名为 rootuser（兼容旧名）
    try:
        _gr = (
            User.query.filter_by(role='super_admin')
            .filter(User.username.in_(['rootuser', 'testuser']))
            .order_by(User.id.asc())
            .first()
        )
        if _gr is not None and not (_gr.nickname or '').strip():
            _gr.nickname = 'GR'
            db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.warning('设置超管昵称 GR 失败', exc_info=True)

    # 反馈#6：启动时清理垃圾箱中超过 30 天的记录（物理删除）
    try:
        _purged = _purge_expired_trash()
        if _purged:
            app.logger.info('启动清理垃圾箱过期记录 %s 条', _purged)
    except Exception:
        app.logger.warning('启动垃圾箱清理失败', exc_info=True)

    # 反馈#7：启动时把 DB（system_settings）中超级管理员维护的 DeepSeek 配置应用到运行时
    try:
        _applied = _apply_deepseek_settings()
        if _applied:
            app.logger.info('已加载 DB 中的 DeepSeek 配置: %s', ','.join(_applied))
    except Exception:
        app.logger.warning('加载 DeepSeek 运行时配置失败', exc_info=True)

    # 反馈#18：启动时把 DB 中 BAGEL 后端（autoDL / 局域网）配置应用到运行时
    try:
        _applied_bagel = _apply_bagel_settings()
        if _applied_bagel:
            app.logger.info('已加载 DB 中的 BAGEL 后端配置: %s', ','.join(_applied_bagel))
    except Exception:
        app.logger.warning('加载 BAGEL 后端运行时配置失败', exc_info=True)

    try:
        from services.messaging import ensure_default_templates, get_or_create_channel_config

        ensure_default_templates()
        get_or_create_channel_config()
        db.session.commit()
    except Exception:
        db.session.rollback()

    # 反馈#12/#13：所有用户默认拥有昵称——回填历史空昵称用户，以及反馈#12 部署时
    # 写入的「用户<ID>」占位昵称；统一改为由用户名派生（如 zhou.yating -> yating，重名追加序号）
    try:
        need_nick = User.query.filter(
            or_(User.nickname.is_(None), User.nickname == '')
        ).all()
        placeholder = [u for u in User.query.all()
                       if (u.nickname or '') and _NICK_PLACEHOLDER_RE.match(u.nickname or '')]
        targets = {u.id: u for u in (need_nick + placeholder)}.values()
        used = {n for (n,) in db.session.query(User.nickname)
                .filter(User.nickname.isnot(None)).all()}
        changed = 0
        for u in targets:
            new_nick = _unique_derived_nickname(u.username, extra_used=used)
            used.add(new_nick)
            if new_nick != (u.nickname or ''):
                u.nickname = new_nick
                changed += 1
        if changed:
            db.session.commit()
            app.logger.info('默认昵称回填/升级：共处理 %d 名用户（空昵称 %d，占位昵称 %d）',
                            changed, len(need_nick), len(placeholder))
    except Exception:
        db.session.rollback()
        app.logger.warning('默认昵称回填失败', exc_info=True)

    # 反馈#14：用户管理字段补全——管理员备注/账号有效期/最近登录时间/算力积分
    # 反馈#15：管理员备注精简为短备注（旧版长备注自动替换，管理员手工填写的自定义备注保留）；
    #          部门去掉区域前缀（华东采集一组 → 采集一组，华北质检中心 → 质检中心）
    try:
        remark_short = {
            'super_admin': '超级管理员',
            'admin': '系统管理员',
            'recorder': '采集处理人员',
            'inspector': '质检审核人员',
        }
        # 反馈#14 生成的长备注前缀（命中即视为系统自动备注，可安全替换为短备注）
        remark_old_long = (
            '超级管理员（系统内置最高权限账号）',
            '系统管理员，负责账号、任务与系统配置管理',
            '数据采集与处理人员，负责现场/互联网采集与转录处理',
            '数据质检审核人员，负责转录结果检查与验收',
        )
        # 反馈#15：部门名区域前缀清洗
        _region_re = re.compile(r'^(华东|华北|华南|华中|西南|西北|东北|海外|东部|西部|南部|北部|中部)')
        default_valid_until = datetime(2027, 12, 31, 23, 59, 59)
        login_base = datetime(2026, 9, 2, 8, 30)
        changed = 0
        for u in User.query.order_by(User.id.asc()).all():
            cur_remark = (u.admin_remark or '').strip()
            if (not cur_remark) or any(cur_remark.startswith(p) for p in remark_old_long):
                u.admin_remark = remark_short.get(u.role, '系统用户')
                changed += 1
            cur_dept = (u.department or '').strip()
            if cur_dept:
                new_dept = _region_re.sub('', cur_dept).strip()
                if new_dept and new_dept != cur_dept:
                    u.department = new_dept
                    changed += 1
            if u.account_valid_until is None:
                u.account_valid_until = default_valid_until
                changed += 1
            if u.last_login_at is None:
                # 确定性补录演示登录时间（集中在 2026-09-02 工作时段），避免列表空值
                u.last_login_at = login_base + timedelta(minutes=(u.id * 37) % 900)
                changed += 1
        db.session.commit()

        # 积分：累计发放不足 1000 的用户补齐到 1000（超管不受积分约束，同样补）
        admin_id = (db.session.query(User.id).filter(User.role == 'super_admin').first() or [None])[0]
        granted = dict(db.session.query(
            UserCreditGrant.user_id, func.coalesce(func.sum(UserCreditGrant.credits), 0)
        ).group_by(UserCreditGrant.user_id).all())
        topped = []
        for u in User.query.order_by(User.id.asc()).all():
            total = int(granted.get(u.id, 0))
            if total < 1000:
                db.session.add(UserCreditGrant(
                    user_id=u.id, credits=1000 - total, period='permanent',
                    reason='系统初始化分配算力积分', granted_by=admin_id))
                topped.append(u.id)
        if topped:
            db.session.commit()
            for uid in topped:
                try:
                    model_redis.delete(credit_service.balance_key(uid))  # 懒重建余额
                except Exception:
                    pass
        if changed or topped:
            app.logger.info('反馈#14 用户字段补全：备注/有效期/登录时间改动 %d 处；积分补发给 %d 个账号 %s',
                            changed, len(topped), topped)
    except Exception:
        db.session.rollback()
        app.logger.warning('用户字段默认值补全失败', exc_info=True)

    # f2 安全：初始超管账号改为环境变量驱动播种；
    # 未设置 SEED_ADMIN_USERNAME / SEED_ADMIN_PASSWORD 时不再创建任何硬编码默认账号，
    # 避免出厂弱口令（testuser/Test@pwd1）在所有部署中通用。
    seed_username = os.environ.get('SEED_ADMIN_USERNAME', '').strip()
    seed_password = os.environ.get('SEED_ADMIN_PASSWORD', '').strip()
    if seed_username and seed_password:
        seeded = User.query.filter_by(username=seed_username).first()
        if not seeded:
            nu = User(
                username=seed_username[:80],
                email=(os.environ.get('SEED_ADMIN_EMAIL', '') or f'{seed_username}@local.invalid')[:120],
                role='super_admin',
                full_name=(os.environ.get('SEED_ADMIN_FULLNAME', '') or '超级管理员')[:80],
                nickname=(os.environ.get('SEED_ADMIN_NICKNAME', '') or '超级管理员')[:80],
                is_active=True,
                is_locked_account=False,
                must_change_password=False,
            )
            nu.set_password(seed_password)
            db.session.add(nu)
            db.session.commit()
            app.logger.info('已按环境变量播种初始超管账号: %s', seed_username)

if __name__ == '__main__':
    # Werkzeug 3 开启 debug 时会优先用 watchdog 热重载；旧版 watchdog 无 EVENT_TYPE_OPENED 会 ImportError
    _use_reloader = True
    try:
        from watchdog.events import EVENT_TYPE_OPENED  # noqa: F401
    except ImportError:
        _use_reloader = False
    app.run(debug=True, use_reloader=_use_reloader)