# MMDAPS 数据表分类与全表结构说明

本文档依据 `models.py` 整理：**六大类业务域说明** + **23 张表逐字段结构**（类型按 SQLAlchemy 映射习惯书写，等价于常见 MySQL/SQLite 语义）。

---

## 一、大类说明（简要）

| 大类 | 包含表数 | 说明 |
|------|----------|------|
| **1. 用户与身份审计** | 3 | 存储登录账号、角色与安全状态；记录登录行为与账号/权限类操作审计，支撑 RBAC 与合规留痕。 |
| **2. 任务与工作流** | 5 | 描述业务任务全生命周期：定义、分配/申领、状态流转日志、公海退领、任务类站内通知（兼容旧表）。 |
| **3. 多模态数据与处理** | 4 | 核心数据资产 `recordings` 及采集元数据、处理结果；`inspections` 为历史检验记录，可与审核并存。 |
| **4. 数据审核（质检）** | 2 | 对单条录音/记录的审核动作留痕（`audit_logs`）及打回等场景下推送给处理人的旧版站内信（`audit_messages`）。 |
| **5. 数据管理与导出** | 4 | 列表筛选模板、自定义数据集与成员关系、导出行为审计，支撑数据管理模块与合规导出。 |
| **6. 消息与系统通知** | 5 | 统一收件箱、投递审计、消息模板、系统公告、渠道配置；与任务/账号/系统类触达联动。 |

---

## 二、全表结构（按大类列出）

以下为每张表：**字段名 | 类型 | 可空 | 默认值/备注 | 约束与索引**

---

### 大类 1：用户与身份审计

#### 表 `users`（用户主表）

| 字段 | 类型 | 可空 | 默认值/备注 | 约束与索引 |
|------|------|------|-------------|------------|
| id | Integer | 否 | 自增 | **PK** |
| username | String(80) | 否 | — | **UNIQUE** |
| email | String(120) | 否 | — | **UNIQUE** |
| password_hash | String(255) | 否 | — | — |
| role | String(20) | 否 | super_admin / admin / recorder / inspector | — |
| created_at | DateTime | 是 | utcnow | — |
| updated_at | DateTime | 是 | 更新时刷新 | — |
| full_name | String(80) | 是 | — | — |
| department | String(128) | 是 | — | — |
| phone | String(32) | 是 | — | — |
| is_active | Boolean | 是 | True | — |
| is_locked_account | Boolean | 是 | False（逻辑注销） | — |
| account_valid_until | DateTime | 是 | 账号有效期 | — |
| must_change_password | Boolean | 是 | False | — |
| failed_login_count | Integer | 是 | 0 | — |
| locked_until | DateTime | 是 | 登录失败锁定截止 | — |
| session_version | Integer | 是 | 0，强制下线时递增 | — |
| last_login_at | DateTime | 是 | — | — |
| last_login_ip | String(45) | 是 | — | — |

---

#### 表 `login_logs`（登录/登出审计）

| 字段 | 类型 | 可空 | 默认值/备注 | 约束与索引 |
|------|------|------|-------------|------------|
| id | Integer | 否 | 自增 | **PK** |
| user_id | Integer | 是 | — | **FK** → users.id，**Index** |
| username_attempted | String(80) | 是 | — | **Index** |
| event_type | String(20) | 否 | login_success / login_fail / logout / idle_timeout / force_logout 等 | **Index** |
| success | Boolean | 是 | False | — |
| ip_address | String(45) | 是 | — | — |
| user_agent | String(512) | 是 | — | — |
| message | String(255) | 是 | — | — |
| created_at | DateTime | 是 | utcnow | **Index** |

---

#### 表 `user_audit_logs`（账号与权限操作审计）

| 字段 | 类型 | 可空 | 默认值/备注 | 约束与索引 |
|------|------|------|-------------|------------|
| id | Integer | 否 | 自增 | **PK** |
| actor_id | Integer | 否 | 操作人 | **FK** → users.id，**Index** |
| target_user_id | Integer | 是 | 被操作用户 | **FK** → users.id，**Index** |
| action_type | String(40) | 否 | 如 create_user、role_change、disable 等 | **Index** |
| detail_json | Text | 是 | JSON 详情 | — |
| created_at | DateTime | 是 | utcnow | **Index** |

---

### 大类 2：任务与工作流

#### 表 `tasks`（任务）

| 字段 | 类型 | 可空 | 默认值/备注 | 约束与索引 |
|------|------|------|-------------|------------|
| id | Integer | 否 | 自增 | **PK** |
| task_no | String(64) | 是 | 任务编号 | **UNIQUE**，**Index** |
| title | String(255) | 否 | — | — |
| description | Text | 是 | — | — |
| acceptance_criteria | Text | 是 | — | — |
| start_date | DateTime | 否 | — | — |
| end_date | DateTime | 否 | — | — |
| required_count | Integer | 是 | 0 | — |
| task_type | Integer | 是 | 兼容旧版 1/2 | — |
| priority | String(16) | 是 | medium | high/medium/low，**Index** |
| task_category | String(20) | 是 | process | collect/process/audit，**Index** |
| task_subtype | String(40) | 是 | — | — |
| assign_mode | String(20) | 是 | manual | auto/manual/pool |
| max_claim_per_user | Integer | 是 | 1 | — |
| workflow_status | String(40) | 是 | draft | **Index** |
| created_by | Integer | 是 | — | **FK** → users.id |
| created_at | DateTime | 是 | utcnow | — |
| published_at | DateTime | 是 | — | — |
| status | String(20) | 是 | 兼容 active/completed/expired | — |
| pause_reason | Text | 是 | — | — |
| terminate_reason | Text | 是 | — | — |
| paused_at | DateTime | 是 | — | — |
| terminated_at | DateTime | 是 | — | — |
| quality_score | Float | 是 | — | — |
| scored_by | Integer | 是 | — | **FK** → users.id |
| scored_at | DateTime | 是 | — | — |
| review_comments | Text | 是 | 复核意见 | — |

---

#### 表 `task_assignments`（任务分配/申领）

| 字段 | 类型 | 可空 | 默认值/备注 | 约束与索引 |
|------|------|------|-------------|------------|
| id | Integer | 否 | 自增 | **PK** |
| task_id | Integer | 是 | — | **FK** → tasks.id，**Index** |
| user_id | Integer | 是 | — | **FK** → users.id，**Index** |
| assigned_at | DateTime | 是 | utcnow | — |
| completed_count | Integer | 是 | 0 | — |
| target_quota | Integer | 是 | 0 | — |
| claimed_from_pool | Boolean | 是 | False | — |
| status | String(20) | 是 | assigned | — |

---

#### 表 `task_workflow_logs`（任务工作流审计）

| 字段 | 类型 | 可空 | 默认值/备注 | 约束与索引 |
|------|------|------|-------------|------------|
| id | Integer | 否 | 自增 | **PK** |
| task_id | Integer | 否 | — | **FK** → tasks.id，**Index** |
| user_id | Integer | 否 | — | **FK** → users.id，**Index** |
| action_type | String(40) | 否 | — | **Index** |
| from_status | String(40) | 是 | — | — |
| to_status | String(40) | 是 | — | — |
| detail_json | Text | 是 | — | — |
| created_at | DateTime | 是 | utcnow | **Index** |

---

#### 表 `task_return_requests`（公海退领申请）

| 字段 | 类型 | 可空 | 默认值/备注 | 约束与索引 |
|------|------|------|-------------|------------|
| id | Integer | 否 | 自增 | **PK** |
| task_id | Integer | 否 | — | **FK** → tasks.id，**Index** |
| user_id | Integer | 否 | 申请人 | **FK** → users.id，**Index** |
| reason | Text | 否 | — | — |
| status | String(20) | 是 | pending | pending/approved/rejected，**Index** |
| reviewed_by | Integer | 是 | — | **FK** → users.id |
| reviewed_at | DateTime | 是 | — | — |
| created_at | DateTime | 是 | utcnow | — |

---

#### 表 `task_messages`（任务相关站内通知·旧版）

| 字段 | 类型 | 可空 | 默认值/备注 | 约束与索引 |
|------|------|------|-------------|------------|
| id | Integer | 否 | 自增 | **PK** |
| recipient_id | Integer | 否 | — | **FK** → users.id，**Index** |
| sender_id | Integer | 否 | — | **FK** → users.id |
| task_id | Integer | 否 | — | **FK** → tasks.id，**Index** |
| title | String(255) | 否 | — | — |
| body | Text | 否 | — | — |
| read_at | DateTime | 是 | — | — |
| created_at | DateTime | 是 | utcnow | **Index** |

---

### 大类 3：多模态数据与处理

#### 表 `recordings`（录音/多模态记录）

| 字段 | 类型 | 可空 | 默认值/备注 | 约束与索引 |
|------|------|------|-------------|------------|
| id | Integer | 否 | 自增 | **PK** |
| filename | String(255) | 否 | — | — |
| file_path | String(512) | 否 | — | — |
| duration | Float | 是 | 秒 | — |
| recorded_by | Integer | 是 | — | **FK** → users.id |
| recorded_at | DateTime | 是 | utcnow | — |
| status | String(20) | 是 | 业务状态 | — |
| text_content | Text | 是 | — | — |
| task_type | Integer | 是 | 兼容 | — |
| created_at | DateTime | 是 | utcnow | — |
| subtitle_srt_path | String(512) | 是 | — | — |
| subtitle_json_path | String(512) | 是 | — | — |
| subtitle_content | Text | 是 | — | — |
| subtitle_json_data | Text | 是 | — | — |
| invalidated_at | DateTime | 是 | 逻辑删除时间 | **Index** |
| invalidated_by | Integer | 是 | — | **FK** → users.id |
| business_task_id | Integer | 是 | 关联任务 | **FK** → tasks.id，**Index** |

---

#### 表 `acquisition_metadata`（采集/上传元数据）

| 字段 | 类型 | 可空 | 默认值/备注 | 约束与索引 |
|------|------|------|-------------|------------|
| id | Integer | 否 | 自增 | **PK** |
| recording_id | Integer | 否 | — | **FK** → recordings.id，**Index** |
| uploader_id | Integer | 否 | — | **FK** → users.id，**Index** |
| uploaded_at | DateTime | 是 | utcnow | **Index** |
| filename | String(255) | 否 | — | — |
| file_type | String(20) | 否 | video/image/audio | — |
| file_size | BigInteger | 否 | 字节 | — |
| file_md5 | String(32) | 否 | — | **Index** |
| task_no | String(64) | 否 | — | **Index** |
| audit_status | String(20) | 是 | pending | pending/pass/pending_fix，**Index** |
| source_channel | String(20) | 否 | upload | upload/record/crawl |

---

#### 表 `processing_results`（处理结果）

| 字段 | 类型 | 可空 | 默认值/备注 | 约束与索引 |
|------|------|------|-------------|------------|
| id | Integer | 否 | 自增 | **PK** |
| recording_id | Integer | 否 | — | **FK** → recordings.id，**Index** |
| processor_id | Integer | 否 | — | **FK** → users.id，**Index** |
| modality | String(20) | 否 | audio/video | — |
| process_mode | String(20) | 否 | manual | manual/algorithm/reviewer_self_fix |
| content_text | Text | 是 | 转写/描述 | — |
| timeline_json | Text | 是 | 时间轴 JSON | — |
| algorithm_raw_json | Text | 是 | — | — |
| processed_at | DateTime | 是 | utcnow | **Index** |

---

#### 表 `inspections`（检验记录·历史/兼容）

| 字段 | 类型 | 可空 | 默认值/备注 | 约束与索引 |
|------|------|------|-------------|------------|
| id | Integer | 否 | 自增 | **PK** |
| recording_id | Integer | 是 | — | **FK** → recordings.id |
| inspector_id | Integer | 是 | — | **FK** → users.id |
| inspection_date | DateTime | 是 | utcnow | — |
| status | String(20) | 是 | approved/rejected | — |
| corrections | Text | 是 | — | — |
| comments | Text | 是 | — | — |

---

### 大类 4：数据审核（质检）

#### 表 `audit_logs`（审核操作留痕）

| 字段 | 类型 | 可空 | 默认值/备注 | 约束与索引 |
|------|------|------|-------------|------------|
| id | Integer | 否 | 自增 | **PK** |
| recording_id | Integer | 否 | — | **FK** → recordings.id，**Index** |
| auditor_id | Integer | 否 | — | **FK** → users.id，**Index** |
| action_type | String(32) | 否 | pass/reject_return/self_fix 等 | — |
| audit_result | String(32) | 否 | approved/pending_fix 等 | — |
| comments | Text | 是 | — | — |
| detail_json | Text | 是 | 结构化详情 | — |
| created_at | DateTime | 是 | utcnow | **Index** |

---

#### 表 `audit_messages`（审核打回等站内信·旧版）

| 字段 | 类型 | 可空 | 默认值/备注 | 约束与索引 |
|------|------|------|-------------|------------|
| id | Integer | 否 | 自增 | **PK** |
| recipient_id | Integer | 否 | — | **FK** → users.id，**Index** |
| sender_id | Integer | 否 | — | **FK** → users.id |
| recording_id | Integer | 是 | — | **FK** → recordings.id，**Index** |
| title | String(255) | 否 | — | — |
| body | Text | 否 | — | — |
| read_at | DateTime | 是 | — | — |
| created_at | DateTime | 是 | utcnow | **Index** |

---

### 大类 5：数据管理与导出

#### 表 `filter_templates`（筛选模板）

| 字段 | 类型 | 可空 | 默认值/备注 | 约束与索引 |
|------|------|------|-------------|------------|
| id | Integer | 否 | 自增 | **PK** |
| user_id | Integer | 否 | — | **FK** → users.id，**Index** |
| name | String(128) | 否 | — | — |
| criteria_json | Text | 否 | 条件 JSON | — |
| created_at | DateTime | 是 | utcnow | — |

---

#### 表 `data_sets`（数据集）

| 字段 | 类型 | 可空 | 默认值/备注 | 约束与索引 |
|------|------|------|-------------|------------|
| id | Integer | 否 | 自增 | **PK** |
| name | String(255) | 否 | — | — |
| description | Text | 是 | — | — |
| version_label | String(32) | 是 | V1.0 | — |
| created_by | Integer | 否 | — | **FK** → users.id，**Index** |
| created_at | DateTime | 是 | utcnow | — |
| updated_at | DateTime | 是 | 更新时刷新 | — |

---

#### 表 `data_set_items`（数据集成员）

| 字段 | 类型 | 可空 | 默认值/备注 | 约束与索引 |
|------|------|------|-------------|------------|
| id | Integer | 否 | 自增 | **PK** |
| dataset_id | Integer | 否 | — | **FK** → data_sets.id，**Index** |
| recording_id | Integer | 否 | — | **FK** → recordings.id，**Index** |
| added_at | DateTime | 是 | utcnow | — |
| — | — | — | — | **UNIQUE**(dataset_id, recording_id) `uq_dataset_recording` |

---

#### 表 `export_logs`（导出审计）

| 字段 | 类型 | 可空 | 默认值/备注 | 约束与索引 |
|------|------|------|-------------|------------|
| id | Integer | 否 | 自增 | **PK** |
| user_id | Integer | 否 | — | **FK** → users.id，**Index** |
| scope_type | String(32) | 否 | filter/dataset/ids 等 | — |
| scope_ref | String(255) | 是 | — | — |
| export_format | String(16) | 否 | — | — |
| fields_json | Text | 是 | 导出字段 JSON | — |
| row_count | Integer | 是 | 0 | — |
| created_at | DateTime | 是 | utcnow | **Index** |

---

### 大类 6：消息与系统通知

#### 表 `inbox_messages`（统一站内信）

| 字段 | 类型 | 可空 | 默认值/备注 | 约束与索引 |
|------|------|------|-------------|------------|
| id | Integer | 否 | 自增 | **PK** |
| recipient_id | Integer | 否 | — | **FK** → users.id，**Index** |
| sender_type | String(24) | 否 | system | system/super_admin/admin/user |
| sender_id | Integer | 是 | — | **FK** → users.id，**Index** |
| category | String(20) | 否 | — | task/audit/account/system，**Index** |
| msg_type | String(64) | 否 | — | **Index** |
| priority | String(16) | 否 | medium | urgent/high/medium/low，**Index** |
| title | String(512) | 否 | — | — |
| summary | String(512) | 是 | — | — |
| body | Text | 否 | — | — |
| business_type | String(32) | 是 | task/recording/user/announcement/none | **Index** |
| business_id | String(64) | 是 | — | **Index** |
| action_url | String(512) | 是 | — | — |
| read_at | DateTime | 是 | — | **Index** |
| pinned_at | DateTime | 是 | — | **Index** |
| user_deleted_at | DateTime | 是 | 个人列表逻辑删除 | **Index** |
| extra_json | Text | 是 | — | — |
| created_at | DateTime | 是 | utcnow | **Index** |

---

#### 表 `message_delivery_logs`（消息投递审计）

| 字段 | 类型 | 可空 | 默认值/备注 | 约束与索引 |
|------|------|------|-------------|------------|
| id | Integer | 否 | 自增 | **PK** |
| inbox_message_id | Integer | 是 | — | **FK** → inbox_messages.id，**Index** |
| recipient_id | Integer | 否 | — | **FK** → users.id，**Index** |
| sender_type | String(24) | 否 | — | — |
| sender_id | Integer | 是 | — | **FK** → users.id |
| msg_type | String(64) | 否 | — | **Index** |
| channel | String(16) | 否 | in_app/email/sms | **Index** |
| status | String(16) | 否 | sent/failed/skipped/pending | **Index** |
| title_snapshot | String(512) | 是 | — | — |
| body_snapshot | Text | 是 | — | — |
| detail | String(512) | 是 | — | — |
| created_at | DateTime | 是 | utcnow | **Index** |

---

#### 表 `message_templates`（消息模板）

| 字段 | 类型 | 可空 | 默认值/备注 | 约束与索引 |
|------|------|------|-------------|------------|
| id | Integer | 否 | 自增 | **PK** |
| template_key | String(64) | 否 | — | **UNIQUE**，**Index** |
| category | String(20) | 否 | — | — |
| title_template | String(512) | 否 | 支持 `{{变量}}` | — |
| body_template | Text | 否 | — | — |
| enabled | Boolean | 是 | True | — |
| default_priority | String(16) | 是 | medium | — |
| description | String(255) | 是 | — | — |
| updated_at | DateTime | 是 | 更新时刷新 | — |

---

#### 表 `system_announcements`（系统公告）

| 字段 | 类型 | 可空 | 默认值/备注 | 约束与索引 |
|------|------|------|-------------|------------|
| id | Integer | 否 | 自增 | **PK** |
| title | String(255) | 否 | — | — |
| body_html | Text | 否 | 富文本 HTML | — |
| priority | String(16) | 否 | medium | — |
| valid_from | DateTime | 是 | — | — |
| valid_until | DateTime | 是 | — | — |
| audience_json | Text | 否 | 受众 JSON | — |
| created_by | Integer | 否 | — | **FK** → users.id |
| created_at | DateTime | 是 | utcnow | **Index** |
| published_at | DateTime | 是 | — | **Index** |
| is_active | Boolean | 是 | True | **Index** |

---

#### 表 `message_channel_config`（消息渠道配置·单例）

| 字段 | 类型 | 可空 | 默认值/备注 | 约束与索引 |
|------|------|------|-------------|------------|
| id | Integer | 否 | 通常固定为 1 | **PK** |
| email_enabled | Boolean | 是 | False | — |
| sms_enabled | Boolean | 是 | False | — |
| email_roles_json | Text | 是 | 可收邮件的角色 JSON 列表 | — |
| updated_at | DateTime | 是 | 更新时刷新 | — |

---

## 三、汇总

| 大类 | 表名 |
|------|------|
| 1. 用户与身份审计 | `users`, `login_logs`, `user_audit_logs` |
| 2. 任务与工作流 | `tasks`, `task_assignments`, `task_workflow_logs`, `task_return_requests`, `task_messages` |
| 3. 多模态数据与处理 | `recordings`, `acquisition_metadata`, `processing_results`, `inspections` |
| 4. 数据审核 | `audit_logs`, `audit_messages` |
| 5. 数据管理与导出 | `filter_templates`, `data_sets`, `data_set_items`, `export_logs` |
| 6. 消息与系统通知 | `inbox_messages`, `message_delivery_logs`, `message_templates`, `system_announcements`, `message_channel_config` |

**共计 23 张表。**

---

*与 `models.py` 保持同步；若模型变更请更新本文档。*
