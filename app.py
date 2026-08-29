from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    send_file,
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
import uuid
import importlib.util
import json
from datetime import datetime, timedelta
from sqlalchemy import func, or_
from models import (
    db,
    User,
    LoginLog,
    UserAuditLog,
    Recording,
    Inspection,
    Task,
    TaskAssignment,
    TaskWorkflowLog,
    TaskReturnRequest,
    TaskMessage,
    AcquisitionMetadata,
    ProcessingResult,
    AuditLog,
    AuditMessage,
    InboxMessage,
    MessageDeliveryLog,
    MessageTemplate,
    SystemAnnouncement,
    MessageChannelConfig,
    FilterTemplate,
    DataSet,
    DataSetItem,
    ExportLog,
)
from config import Config
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from services.bagel_queue import BagelQueueService
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
)

def _is_system_admin():
    return current_user.is_authenticated and current_user.role in ('admin', 'super_admin')


def _is_super_admin():
    return current_user.is_authenticated and current_user.role == 'super_admin'


def _log_login_event(user_id, username_attempted, success, event_type, message=None):
    try:
        row = LoginLog(
            user_id=user_id,
            username_attempted=username_attempted or '',
            event_type=event_type,
            success=success,
            ip_address=request.remote_addr,
            user_agent=(request.user_agent.string or '')[:500] if request.user_agent else '',
            message=(message or '')[:250],
        )
        db.session.add(row)
        db.session.commit()
    except Exception:
        db.session.rollback()


def _log_user_audit(actor_id, target_user_id, action_type, detail=None):
    row = UserAuditLog(
        actor_id=actor_id,
        target_user_id=target_user_id,
        action_type=action_type,
        detail_json=json.dumps(detail, ensure_ascii=False) if detail else None,
    )
    db.session.add(row)


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
    date_part = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    rand_part = uuid.uuid4().hex[:8].upper()
    return f"{prefix}-{date_part}-{rand_part}"


def create_acquisition_metadata(recording, uploader_id, source_channel='upload'):
    file_size = os.path.getsize(recording.file_path) if os.path.exists(recording.file_path) else 0
    file_md5 = compute_file_md5(recording.file_path) if os.path.exists(recording.file_path) else ''
    metadata = AcquisitionMetadata(
        recording_id=recording.id,
        uploader_id=uploader_id,
        uploaded_at=datetime.utcnow(),
        filename=recording.filename,
        file_type=detect_file_type(recording.filename),
        file_size=file_size,
        file_md5=file_md5,
        task_no=generate_task_no('COLLECT'),
        audit_status='pending',
        source_channel=source_channel,
    )
    db.session.add(metadata)
    return metadata


def _is_reviewer():
    return current_user.is_authenticated and current_user.role in ('inspector', 'admin', 'super_admin')


def _latest_processing_result(recording_id):
    return (
        ProcessingResult.query.filter_by(recording_id=recording_id)
        .order_by(ProcessingResult.processed_at.desc())
        .first()
    )


def _sync_acquisition_audit(recording, audit_status):
    meta = AcquisitionMetadata.query.filter_by(recording_id=recording.id).first()
    if meta:
        meta.audit_status = audit_status


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

    emit_audit_inbox(
        [processor_id],
        current_user.id,
        recording.id,
        recording.filename or '',
        comments or '',
    )
    msg = AuditMessage(
        recipient_id=processor_id,
        sender_id=current_user.id,
        recording_id=recording.id,
        title='审核打回：请修正数据',
        body=(
            f'录制 ID {recording.id}（{recording.filename}）未通过审核，状态已设为「待修正」。\n'
            f'审核意见：\n{comments}'
        ),
    )
    db.session.add(msg)


def _build_audit_preview(recording):
    """审核页：来源、处理人、元数据、多模态预览字段、合规性标记。"""
    meta = AcquisitionMetadata.query.filter_by(recording_id=recording.id).first()
    pr = _latest_processing_result(recording.id)
    processor = User.query.get(pr.processor_id) if pr else None
    uploader = User.query.get(meta.uploader_id) if meta else None
    timeline = []
    if pr and pr.timeline_json:
        try:
            timeline = json.loads(pr.timeline_json)
        except (json.JSONDecodeError, TypeError):
            timeline = []
    file_ok = bool(recording.file_path and os.path.isfile(recording.file_path))
    meta_ok = bool(
        meta
        and meta.file_md5
        and meta.task_no
        and meta.uploader_id
        and meta.uploaded_at
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
            'md5': meta.file_md5 if meta else None,
            'task_no': meta.task_no if meta else None,
            'source_channel': meta.source_channel if meta else None,
        },
        'processor': {'id': processor.id, 'username': processor.username} if processor else None,
        'uploader': {'id': uploader.id, 'username': uploader.username} if uploader else None,
        'uploaded_at': meta.uploaded_at.isoformat() if meta and meta.uploaded_at else None,
        'last_processed_at': pr.processed_at.isoformat() if pr and pr.processed_at else None,
        'process_mode': pr.process_mode if pr else None,
    }


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

            filename = secure_filename(file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)

            recording = Recording(
                filename=filename,
                file_path=filepath,
                recorded_by=current_user.id,
                status='pending'
            )
            db.session.add(recording)
            db.session.flush()  # get recording.id
            create_acquisition_metadata(recording, current_user.id, source_channel='upload')
            created_ids.append(recording.id)

        db.session.commit()
        return jsonify({'success': True, 'recording_ids': created_ids, 'count': len(created_ids)})
    
    lang = get_language()
    return render_template(f'upload_{lang}.html')

# 2. Data Processing Module
@app.route('/process')
@login_required
def process():
    recordings = Recording.query.filter_by(status='pending').all()
    lang = get_language()
    return render_template(f'process_{lang}.html', recordings=recordings)

# API Endpoints for processing module
@app.route('/api/process/next-task')
@login_required
def get_next_task():
    task_type = request.args.get('type', type=int)
    
    # Get pending recordings based on task type
    if task_type == 1:
        # Type 1: Inspection tasks - recordings that need inspection
        recording = Recording.query.filter_by(status='pending', task_type=1).first()
    else:
        # Type 2: Transcription tasks - recordings that need transcription
        recording = Recording.query.filter_by(status='pending', task_type=2).first()
    
    if recording:
        # Create video URL
        video_url = f'/uploads/{recording.filename}'
        
        task_data = {
            'id': recording.id,
            'video_url': video_url,
            'text': recording.text_content or ''
        }
        return jsonify({'task': task_data})
    else:
        return jsonify({'task': None})

@app.route('/api/process/inspect', methods=['POST'])
@login_required
def process_inspection():
    """兼容旧版「类型1检查」；优先按 recording_id 定位，否则取首条 pending+task_type=1。"""
    data = request.get_json(silent=True) or {}
    recording_id = data.get('recording_id')
    action = data.get('action')  # 'approve' or 'reject'
    corrections = data.get('corrections', '')
    comments = data.get('comments', '')

    if recording_id:
        recording = Recording.query.get(recording_id)
        if not recording or recording.status != 'pending' or recording.task_type != 1:
            return jsonify({'error': '录音不存在或不是待检查状态'}), 404
    else:
        recording = Recording.query.filter_by(status='pending', task_type=1).first()
        if not recording:
            return jsonify({'error': '没有找到待检查的录音'}), 404

    inspection = Inspection(
        recording_id=recording.id,
        inspector_id=current_user.id,
        status=action,
        corrections=corrections,
        comments=comments,
    )
    db.session.add(inspection)
    recording.status = 'completed' if action == 'approve' else 'rejected'
    if corrections:
        recording.text_content = corrections
    if action == 'approve':
        _sync_acquisition_audit(recording, 'pass')
    _append_audit_log(
        recording.id,
        'legacy_inspect',
        'approved' if action == 'approve' else 'rejected',
        comments=comments or corrections,
        detail={'legacy_action': action},
    )
    db.session.commit()
    return jsonify({'success': True, 'message': '检查提交成功'})

@app.route('/api/process/transcribe', methods=['POST'])
@login_required
def process_transcription():
    data = request.get_json()
    transcription = data.get('transcription', '')
    
    # Find the recording (you might want to get this from the current task context)
    recording = Recording.query.filter_by(status='pending', task_type=2).first()
    
    if recording:
        # Update recording with transcription
        recording.text_content = transcription
        recording.status = 'pending_review'
        _sync_acquisition_audit(recording, 'pending')

        # Save structured processing result
        timeline = data.get('timeline', [])
        result_row = ProcessingResult(
            recording_id=recording.id,
            processor_id=current_user.id,
            modality=detect_modality(recording.filename),
            process_mode='manual',
            content_text=transcription,
            timeline_json=json.dumps(timeline, ensure_ascii=False) if timeline else '[]'
        )
        db.session.add(result_row)
        
        db.session.commit()
        
        return jsonify({'success': True, 'message': '转录保存成功，状态已更新为待审核'})
    else:
        return jsonify({'error': '没有找到待转录的录音'}), 404


@app.route('/api/process/next-task-v2')
@login_required
def get_next_task_v2():
    """
    Role + modality based task allocation for data processing module.
    query:
      - modality: audio|video (optional)
      - mode: manual|algorithm (optional, reserved)
    """
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
        if modality in {'audio', 'video'} and r_mod != modality:
            return False
        if current_user.role == 'inspector':
            return True
        return True

    matched = [r for r in candidates if _match_role_and_modality(r)]
    if not matched:
        return jsonify({'task': None})

    recording = matched[0]
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
    data = request.get_json(silent=True) or {}
    recording_id = data.get('recording_id')
    content_text = data.get('content_text', '')
    timeline = data.get('timeline', [])

    if not recording_id:
        return jsonify({'error': 'recording_id is required'}), 400
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

    modality = data.get('modality') or detect_modality(recording.filename)
    if modality not in {'audio', 'video'}:
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
    return jsonify({'success': True, 'message': '手动处理结果已写回，状态已更新为待审核'})


@app.route('/api/process/algorithm-submit', methods=['POST'])
@login_required
def process_algorithm_submit():
    """
    Submit algorithm task (BAGEL) and return async task_id.
    """
    data = request.get_json(silent=True) or {}
    recording_id = data.get('recording_id')
    if not recording_id:
        return jsonify({'error': 'recording_id is required'}), 400
    recording = Recording.query.get(recording_id)
    if not recording:
        return jsonify({'error': 'recording not found'}), 404

    if not bagel_queue.health():
        return jsonify({'error': 'algorithm queue unavailable'}), 503

    payload = {
        'recording_id': recording.id,
        'file_path': recording.file_path,
        'filename': recording.filename,
        'modality': detect_modality(recording.filename),
        'submitted_by': current_user.id,
        'options': data.get('options', {}),
        'bagel_service_url': app.config['BAGEL_SERVICE_URL'],
    }
    task_id = bagel_queue.enqueue(payload)
    return jsonify({'success': True, 'task_id': task_id, 'status': 'queued'})


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
        .order_by(Recording.created_at.asc())
        .limit(200)
        .all()
    )
    items = []
    for r in rows:
        meta = AcquisitionMetadata.query.filter_by(recording_id=r.id).first()
        pr = _latest_processing_result(r.id)
        pu = User.query.get(pr.processor_id) if pr else None
        uu = None
        if meta and meta.uploader_id:
            uu_obj = User.query.get(meta.uploader_id)
            uu = uu_obj.username if uu_obj else None
        elif r.recorded_by:
            uu_obj = User.query.get(r.recorded_by)
            uu = uu_obj.username if uu_obj else None
        items.append(
            {
                'id': r.id,
                'filename': r.filename,
                'modality': detect_modality(r.filename),
                'task_no': meta.task_no if meta else None,
                'uploader_username': uu,
                'processor_username': pu.username if pu else None,
                'created_at': r.created_at.isoformat() if r.created_at else None,
            }
        )
    return jsonify({'items': items})


@app.route('/api/audit/next')
@login_required
def audit_next():
    if not _is_reviewer():
        return jsonify({'error': 'Forbidden'}), 403
    recording = (
        Recording.query.filter(Recording.status == 'pending_review')
        .order_by(Recording.created_at.asc())
        .first()
    )
    if not recording:
        return jsonify({'task': None})
    return jsonify({'task': _build_audit_preview(recording)})


@app.route('/api/audit/detail/<int:recording_id>')
@login_required
def audit_detail(recording_id):
    if not _is_reviewer():
        return jsonify({'error': 'Forbidden'}), 403
    recording = Recording.query.get(recording_id)
    if not recording:
        return jsonify({'error': 'not found'}), 404
    if recording.status != 'pending_review':
        return jsonify({'error': '该条不在待审核队列'}), 400
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
    _sync_acquisition_audit(recording, 'pass')
    _append_audit_log(
        recording.id,
        'pass',
        'approved',
        comments=data.get('comments', ''),
        detail={'note': 'audit approved and archived (logical compliance store)'},
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
    _sync_acquisition_audit(recording, 'pending_fix')
    _append_audit_log(
        recording.id,
        'reject_return',
        'pending_fix',
        comments=comments,
        detail={'processor_id': processor_id},
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
    _sync_acquisition_audit(recording, 'pass')
    _append_audit_log(
        recording.id,
        'self_fix',
        'approved',
        comments=data.get('comments', ''),
        detail={'timeline_segments': len(timeline or [])},
    )
    db.session.commit()
    return jsonify({'success': True, 'message': '已保存修正并通过审核'})


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
    """兼容旧接口：仅审核类历史站内信。"""
    rows = (
        AuditMessage.query.filter_by(recipient_id=current_user.id)
        .order_by(AuditMessage.created_at.desc())
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
                'recording_id': m.recording_id,
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

    if include_legacy:
        for m in AuditMessage.query.filter_by(recipient_id=current_user.id).order_by(AuditMessage.created_at.desc()).limit(200).all():
            if read_filter == 'read' and not m.read_at:
                continue
            if read_filter == 'unread' and m.read_at:
                continue
            if category and category != 'audit':
                continue
            if qtext and qtext not in (m.title or '') and qtext not in (m.body or ''):
                continue
            items.append(
                {
                    'msg_key': f'audit:{m.id}',
                    'source': 'audit',
                    'id': m.id,
                    'category': 'audit',
                    'msg_type': 'audit_reject_legacy',
                    'priority': 'high',
                    'title': m.title,
                    'summary': (m.body or '')[:200],
                    'body': m.body,
                    'business_type': 'recording',
                    'business_id': str(m.recording_id or ''),
                    'action_url': '/manage',
                    'read_at': m.read_at.isoformat() if m.read_at else None,
                    'pinned_at': None,
                    'created_at': m.created_at.isoformat() if m.created_at else None,
                    'sender_type': 'user',
                }
            )
        for m in TaskMessage.query.filter_by(recipient_id=current_user.id).order_by(TaskMessage.created_at.desc()).limit(200).all():
            if read_filter == 'read' and not m.read_at:
                continue
            if read_filter == 'unread' and m.read_at:
                continue
            if category and category != 'task':
                continue
            if qtext and qtext not in (m.title or '') and qtext not in (m.body or ''):
                continue
            items.append(
                {
                    'msg_key': f'task:{m.id}',
                    'source': 'task',
                    'id': m.id,
                    'category': 'task',
                    'msg_type': 'task_legacy',
                    'priority': 'medium',
                    'title': m.title,
                    'summary': (m.body or '')[:200],
                    'body': m.body,
                    'business_type': 'task',
                    'business_id': str(m.task_id),
                    'action_url': f'/tasks?highlight={m.task_id}',
                    'read_at': m.read_at.isoformat() if m.read_at else None,
                    'pinned_at': None,
                    'created_at': m.created_at.isoformat() if m.created_at else None,
                    'sender_type': 'user',
                }
            )

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
    n_audit = AuditMessage.query.filter_by(recipient_id=current_user.id).filter(AuditMessage.read_at.is_(None)).count()
    n_task = TaskMessage.query.filter_by(recipient_id=current_user.id).filter(TaskMessage.read_at.is_(None)).count()
    return jsonify({'count': n_inbox + n_audit + n_task, 'inbox': n_inbox, 'legacy_audit': n_audit, 'legacy_task': n_task})


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
    AuditMessage.query.filter_by(recipient_id=current_user.id).filter(AuditMessage.read_at.is_(None)).update(
        {AuditMessage.read_at: now}, synchronize_session=False
    )
    TaskMessage.query.filter_by(recipient_id=current_user.id).filter(TaskMessage.read_at.is_(None)).update(
        {TaskMessage.read_at: now}, synchronize_session=False
    )
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
    data = request.get_json(silent=True) or {}
    source = data.get('source')
    mid = data.get('id')
    try:
        mid = int(mid)
    except (TypeError, ValueError):
        return jsonify({'error': 'bad id'}), 400
    now = datetime.utcnow()
    if source == 'audit':
        m = AuditMessage.query.get(mid)
        if not m or m.recipient_id != current_user.id:
            return jsonify({'error': 'not found'}), 404
        m.read_at = now
    elif source == 'task':
        m = TaskMessage.query.get(mid)
        if not m or m.recipient_id != current_user.id:
            return jsonify({'error': 'not found'}), 404
        m.read_at = now
    else:
        return jsonify({'error': 'bad source'}), 400
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/messages/<int:message_id>/read', methods=['POST'])
@login_required
def mark_message_read(message_id):
    m = AuditMessage.query.get(message_id)
    if not m or m.recipient_id != current_user.id:
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
    return send_file(os.path.join(app.config['UPLOAD_FOLDER'], filename))

# 3. Data Management Module
@app.route('/manage')
@login_required
def manage():
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
    try:
        chunk = request.files['chunk']
        chunk_number = int(request.form['chunk_number'])
        total_chunks = int(request.form['total_chunks'])
        original_filename = request.form['filename']
        
        # 更好的文件名处理：保留原始文件名，只清理危险字符
        def clean_filename(filename):
            # 移除路径分隔符和危险字符
            dangerous_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|']
            for char in dangerous_chars:
                filename = filename.replace(char, '_')
            return filename
        
        # 清理文件名但保留原始名称
        filename = clean_filename(original_filename)
        
        # 如果文件名为空，使用时间戳
        if not filename or filename.strip() == '':
            file_ext = os.path.splitext(original_filename)[1]
            filename = f"upload_{int(time.time())}{file_ext}"
        
        if not allowed_file(filename):
            return jsonify({'error': f'Unsupported file type: {filename}'}), 400

        print(f"Upload chunk: {chunk_number}/{total_chunks} for file: {filename}")
        
        temp_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'temp', str(current_user.id))
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir)
        
        chunk_path = os.path.join(temp_dir, f"{filename}.part{chunk_number}")
        chunk.save(chunk_path)
        
        if chunk_number == total_chunks - 1:
            # Combine all chunks
            final_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            print(f"Combining chunks into: {final_path}")
            
            with open(final_path, 'wb') as outfile:
                for i in range(total_chunks):
                    chunk_path = os.path.join(temp_dir, f"{filename}.part{i}")
                    with open(chunk_path, 'rb') as infile:
                        outfile.write(infile.read())
                    os.remove(chunk_path)
            
            # Create recording entry
            recording = Recording(
                filename=filename,
                file_path=final_path,
                recorded_by=current_user.id,
                status='pending'
            )
            db.session.add(recording)
            db.session.flush()  # get recording id before metadata
            create_acquisition_metadata(recording, current_user.id, source_channel='record')
            db.session.commit()
            
            print(f"Upload completed: recording_id={recording.id}")
            return jsonify({'success': True, 'recording_id': recording.id})
        
        return jsonify({'success': True, 'chunk_received': chunk_number})
    except Exception as e:
        print(f"Upload error: {str(e)}")
        return jsonify({'error': str(e)}), 500

# API Endpoints for Data Management
@app.route('/api/dashboard/stats')
@login_required
def dashboard_stats():
    return jsonify(compute_dashboard_stats())


@app.route('/api/manage/test')
@login_required
def test_manage_data():
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
    try:
        args = normalize_manage_args(request.args.to_dict(flat=True))
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


@app.route('/api/manage/preview/<int:recording_id>')
@login_required
def preview_recording(recording_id):
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
    recording = Recording.query.get_or_404(recording_id)
    if os.path.exists(recording.file_path):
        return send_file(recording.file_path, as_attachment=True, download_name=recording.filename)
    return jsonify({'error': '文件不存在'}), 404


@app.route('/api/manage/batch-download', methods=['POST'])
@login_required
def batch_download_recordings():
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
        log = ExportLog(
            user_id=current_user.id,
            scope_type='legacy_export',
            scope_ref=data_type,
            export_format=export_format,
            fields_json=json.dumps(fields, ensure_ascii=False),
            row_count=len(rows),
        )
        db.session.add(log)
        db.session.commit()
        return export_as_response(rows, export_format, prefix)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/manage/export-v2', methods=['POST'])
@login_required
def export_manage_v2():
    body = request.get_json(silent=True) or {}
    fmt = (body.get('format') or 'json').lower()
    if fmt not in ('json', 'csv'):
        return jsonify({'error': 'format 仅支持 json 或 csv'}), 400
    scope = (body.get('scope') or 'filter').lower()
    fields = body.get('fields') or ['filename', 'status', 'text_content', 'created_at']
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
        id_list = body.get('recording_ids') or []
        scope_ref = f'ids:{len(id_list)}'
        recs = Recording.query.filter(Recording.id.in_(id_list)).all() if id_list else []
    else:
        fargs = normalize_manage_args(body.get('filters') or {})
        recs = manage_records_base_query(fargs).all()
        scope_ref = 'filter'
    rows = build_export_rows(recs, fields)
    log = ExportLog(
        user_id=current_user.id,
        scope_type=scope,
        scope_ref=scope_ref[:255],
        export_format=fmt,
        fields_json=json.dumps(fields, ensure_ascii=False),
        row_count=len(rows),
    )
    db.session.add(log)
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
    ds = DataSet(
        name=name,
        description=data.get('description', '') or '',
        version_label=ver,
        created_by=current_user.id,
    )
    db.session.add(ds)
    db.session.commit()
    return jsonify({'success': True, 'id': ds.id})


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


@app.route('/api/manage/datasets/<int:dsid>/items', methods=['POST', 'DELETE'])
@login_required
def datasets_items(dsid):
    ds = DataSet.query.get_or_404(dsid)
    if not _is_system_admin() and ds.created_by != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403
    body = request.get_json(silent=True) or {}
    ids = body.get('recording_ids') or []
    if request.method == 'POST':
        for rid in ids:
            if not Recording.query.get(rid):
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


# API Endpoints for Crawl-based Data Collection
@app.route('/api/crawl/search', methods=['POST'])
@login_required
def crawl_search():
    """
    Search bilibili videos by keywords.
    body: {"keywords":"采访", "count":10, "duration_limit":30}
    """
    try:
        data = request.get_json(silent=True) or {}
        keywords_raw = data.get('keywords', '')
        count = int(data.get('count', 10))
        duration_limit = int(data.get('duration_limit', 30))
        count = max(1, min(count, 30))
        if count not in {10, 20, 30}:
            count = 10

        if not keywords_raw:
            return jsonify({'error': 'keywords is required'}), 400

        if isinstance(keywords_raw, str):
            keywords = [k.strip() for k in keywords_raw.split(',') if k.strip()]
        else:
            keywords = [str(k).strip() for k in keywords_raw if str(k).strip()]
        if not keywords:
            return jsonify({'error': 'keywords is empty'}), 400

        Crawler = load_bilibili_crawler_class()
        crawler = Crawler()
        results = crawler.search_videos(keywords=keywords, max_count=count, duration_limit=duration_limit)
        return jsonify({'success': True, 'count': len(results), 'items': results})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/crawl/download', methods=['POST'])
@login_required
def crawl_download():
    """
    Download selected search results and register into DB.
    body: {"items":[...video_info...]}
    """
    try:
        data = request.get_json(silent=True) or {}
        items = data.get('items', [])
        if not items:
            return jsonify({'error': 'items is required'}), 400

        Crawler = load_bilibili_crawler_class()
        crawler = Crawler()
        crawler.data = items
        crawler.download_videos(max_download=len(items))

        created_ids = []
        for video in crawler.data:
            local_path = video.get('本地路径')
            if not local_path or not os.path.exists(local_path):
                continue
            filename = os.path.basename(local_path)
            # skip if already recorded
            exists = Recording.query.filter_by(file_path=os.path.abspath(local_path)).first()
            if exists:
                continue
            recording = Recording(
                filename=filename,
                file_path=os.path.abspath(local_path),
                recorded_by=current_user.id,
                status='pending'
            )
            db.session.add(recording)
            db.session.flush()
            create_acquisition_metadata(recording, current_user.id, source_channel='crawl')
            created_ids.append(recording.id)

        db.session.commit()
        return jsonify({'success': True, 'created_count': len(created_ids), 'recording_ids': created_ids})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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
    total_videos = len(recordings)
    total_duration = sum(float(r.duration or 0) for r in recordings)

    task_rows = Task.query.order_by(Task.created_at.desc()).limit(200).all()
    task_list = []
    for t in task_rows:
        s = serialize_task_summary(t)
        s['task_type'] = t.task_type
        s['start_date'] = t.start_date.isoformat() if t.start_date else None
        s['end_date'] = t.end_date.isoformat() if t.end_date else None
        s['status'] = effective_workflow_status(t)
        task_list.append(s)

    users = User.query.all()
    user_performance = []
    for u in users:
        vrec = Recording.query.filter_by(recorded_by=u.id).count()
        dur_sum = (
            db.session.query(func.coalesce(func.sum(Recording.duration), 0))
            .filter(Recording.recorded_by == u.id)
            .scalar()
            or 0
        )
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
                'videos_recorded': vrec,
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


# ---------- 任务工作流 API（发起 / 申领 / 执行 / 复核 / 评分 / 归档）----------
@app.route('/api/workflow/tasks', methods=['GET', 'POST'])
@login_required
def workflow_tasks():
    if request.method == 'GET':
        scope = request.args.get('scope', 'all')
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
        return jsonify({'error': '仅管理员或采集员（发起人）可创建任务'}), 403
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
        uids = [int(x) for x in (body.get('user_ids') or []) if str(x).isdigit()]
        if not uids:
            return jsonify({'error': '手动分配需提供 user_ids'}), 400
        n = len(uids)
        base = task.required_count // n
        extra = task.required_count % n
        for i, uid in enumerate(uids):
            if not User.query.get(uid):
                continue
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
    db.session.commit()
    return jsonify({'success': True, 'workflow_status': task.workflow_status})


@app.route('/api/workflow/tasks/<int:task_id>/claim', methods=['POST'])
@login_required
def workflow_task_claim(task_id):
    task = Task.query.get_or_404(task_id)
    if task.assign_mode != 'pool' or effective_workflow_status(task) != 'pending_claim':
        return jsonify({'error': '非可申领状态'}), 400
    need_role = CATEGORY_ROLE.get(task.task_category, 'recorder')
    if current_user.role not in (need_role, 'admin', 'super_admin'):
        return jsonify({'error': '角色不可申领该任务'}), 403
    if TaskAssignment.query.filter_by(task_id=task.id, user_id=current_user.id).first():
        return jsonify({'error': '已申领过'}), 400
    quota_sum = total_quota_for_task(task.id)
    remaining = task.required_count - quota_sum
    if remaining <= 0:
        return jsonify({'error': '任务已被领完'}), 400
    take = min(task.max_claim_per_user or 1, remaining)
    prev = task.workflow_status
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
    task.workflow_status = 'pending_execute'
    sync_legacy_status(task)
    log_task_action(task, current_user.id, 'claim', prev, task.workflow_status, {'quota': take})
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
    if prev in ('archived', 'terminated', 'completed'):
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
    rows = (
        TaskMessage.query.filter_by(recipient_id=current_user.id)
        .order_by(TaskMessage.created_at.desc())
        .limit(50)
        .all()
    )
    return jsonify(
        {
            'items': [
                {
                    'id': m.id,
                    'task_id': m.task_id,
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
    if 'department' in data:
        user.department = (data['department'] or '')[:128]
    if 'account_valid_until' in data:
        raw = data.get('account_valid_until')
        user.account_valid_until = _parse_task_datetime(raw) if raw else None
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


@app.route('/api/users/<int:user_id>/reset-password', methods=['POST'])
@login_required
def reset_user_password(user_id):
    if not _is_system_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    user = User.query.get_or_404(user_id)
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
    return jsonify({'success': True, 'temporary_password': pwd, 'message': '请通过安全渠道告知用户，用户首次登录须修改密码'})


@app.route('/api/users/<int:user_id>/force-logout', methods=['POST'])
@login_required
def force_user_logout(user_id):
    if not _is_system_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    user = User.query.get_or_404(user_id)
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
        department=(data.get('department') or '')[:128],
        phone=(data.get('phone') or '')[:32],
        is_active=True,
        is_locked_account=False,
        must_change_password=must_change,
    )
    av = data.get('account_valid_until')
    if av:
        new_user.account_valid_until = _parse_task_datetime(av)
    new_user.set_password(pwd)
    db.session.add(new_user)
    db.session.flush()
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
    if data.get('return_initial_password') or not (data.get('password') or '').strip():
        out['initial_password'] = pwd
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
    """当前用户查看本人登录/登出记录（非管理员接口）。"""
    rows = (
        LoginLog.query.filter(LoginLog.user_id == current_user.id)
        .order_by(LoginLog.created_at.desc())
        .limit(100)
        .all()
    )
    return jsonify(
        {
            'items': [
                {
                    'event_type': x.event_type,
                    'success': x.success,
                    'ip': x.ip_address,
                    'at': x.created_at.isoformat() if x.created_at else None,
                    'message': x.message,
                }
                for x in rows
            ]
        }
    )


@app.route('/api/profile/my-audit-trail', methods=['GET'])
@login_required
def profile_my_audit_trail():
    """与当前账号相关的权限/密码/资料类审计摘要（本人可见）。"""
    q = UserAuditLog.query.filter(
        or_(
            UserAuditLog.target_user_id == current_user.id,
            UserAuditLog.actor_id == current_user.id,
        )
    )
    rows = q.order_by(UserAuditLog.created_at.desc()).limit(100).all()
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
    uid = request.args.get('user_id', type=int)
    if not _is_system_admin():
        uid = current_user.id
    q = LoginLog.query
    if uid:
        q = q.filter(LoginLog.user_id == uid)
    rows = q.order_by(LoginLog.created_at.desc()).limit(500).all()
    return jsonify(
        {
            'items': [
                {
                    'id': x.id,
                    'user_id': x.user_id,
                    'username': x.username_attempted,
                    'event_type': x.event_type,
                    'success': x.success,
                    'ip': x.ip_address,
                    'at': x.created_at.isoformat() if x.created_at else None,
                    'message': x.message,
                }
                for x in rows
            ]
        }
    )


@app.route('/api/audit/user-actions')
@login_required
def audit_user_actions():
    if not _is_system_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    rows = UserAuditLog.query.order_by(UserAuditLog.created_at.desc()).limit(500).all()
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
    if not _is_system_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    logs = LoginLog.query.order_by(LoginLog.created_at.desc()).limit(2000).all()
    audits = UserAuditLog.query.order_by(UserAuditLog.created_at.desc()).limit(2000).all()
    payload = {
        'login_logs': [
            {
                'user_id': x.user_id,
                'username': x.username_attempted,
                'event': x.event_type,
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
    if not bagel_queue.health():
        return jsonify({'error': 'Redis queue is unavailable'}), 503

    data = request.get_json(silent=True) or {}
    recording_id = data.get('recording_id')
    if not recording_id:
        return jsonify({'error': 'recording_id is required'}), 400

    recording = Recording.query.get(recording_id)
    if not recording:
        return jsonify({'error': 'recording not found'}), 404

    payload = {
        'recording_id': recording.id,
        'file_path': recording.file_path,
        'filename': recording.filename,
        'submitted_by': current_user.id,
        'options': data.get('options', {}),
        'bagel_service_url': app.config['BAGEL_SERVICE_URL'],
    }
    task_id = bagel_queue.enqueue(payload)
    return jsonify({'success': True, 'task_id': task_id, 'status': 'queued'})


@app.route('/api/algorithm/bagel/status/<task_id>')
@login_required
def bagel_task_status(task_id):
    if not bagel_queue.health():
        return jsonify({'error': 'Redis cache is unavailable'}), 503
    task = bagel_queue.get_task(task_id)
    if not task:
        return jsonify({'error': 'task not found'}), 404
    return jsonify({'success': True, 'task': task})

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        user = User.query.filter_by(username=username).first()
        if not user:
            _log_login_event(None, username, False, 'login_fail', '用户不存在')
            flash('用户名或密码错误', 'danger')
            return redirect(url_for('login'))
        if user.is_locked_account or not user.is_active:
            _log_login_event(user.id, username, False, 'login_fail', '账号已禁用或已注销')
            flash('账号不可用', 'danger')
            return redirect(url_for('login'))
        if user.account_valid_until and user.account_valid_until < datetime.utcnow():
            _log_login_event(user.id, username, False, 'login_fail', '账号已过期')
            flash('账号已过期', 'danger')
            return redirect(url_for('login'))
        if user.locked_until and user.locked_until > datetime.utcnow():
            _log_login_event(user.id, username, False, 'login_fail', '账号已锁定')
            flash('登录尝试过多，请稍后再试', 'warning')
            return redirect(url_for('login'))
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
        user.last_login_ip = request.remote_addr
        db.session.commit()
        login_user(user, remember=False)
        session.permanent = True
        session['_sv'] = user.session_version or 0
        session['_idle_ts'] = time.time()
        _log_login_event(user.id, username, True, 'login_success', None)
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


@app.route('/logout')
@login_required
def logout():
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
    try:
        from services.messaging import ensure_default_templates, get_or_create_channel_config

        ensure_default_templates()
        get_or_create_channel_config()
        db.session.commit()
    except Exception:
        db.session.rollback()

    test_user = User.query.filter_by(username='testuser').first()
    if not test_user:
        nu = User(
            username='testuser',
            email='test@example.com',
            role='super_admin',
            full_name='GR',
            is_active=True,
            is_locked_account=False,
            must_change_password=False,
        )
        nu.set_password('Test@pwd1')
        db.session.add(nu)
        db.session.commit()
    elif (
        test_user.role == 'super_admin'
        and test_user.full_name in (None, '', '超级管理员')
    ):
        test_user.full_name = 'GR'
        db.session.commit()

if __name__ == '__main__':
    # Werkzeug 3 开启 debug 时会优先用 watchdog 热重载；旧版 watchdog 无 EVENT_TYPE_OPENED 会 ImportError
    _use_reloader = True
    try:
        from watchdog.events import EVENT_TYPE_OPENED  # noqa: F401
    except ImportError:
        _use_reloader = False
    app.run(debug=True, use_reloader=_use_reloader)