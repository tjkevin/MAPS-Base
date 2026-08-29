# MAPS / MMDAPS 功能模块划分（配图代码）

以下均为 **Mermaid** 源码，可复制到支持 Mermaid 的 Markdown 预览、Typora、Notion、GitLab/GitHub 或 [mermaid.live](https://mermaid.live) 中渲染。

---

## 图 1：模块分解（按业务域）

```mermaid
flowchart TB
  subgraph M1["① 门户与总览"]
    A1["/ 首页 · /api/dashboard/stats"]
  end

  subgraph M2["② 认证与个人中心"]
    A2["/login · /logout · /profile"]
    A2b["/account/first-password · /api/profile/*"]
  end

  subgraph M3["③ 数据采集"]
    A3["/upload · /api/upload/stream"]
    A3b["/api/crawl/search · /api/crawl/download"]
  end

  subgraph M4["④ 数据处理"]
    A4["/process · /api/process/*"]
    A4b["/api/algorithm/bagel/* · Redis 队列"]
  end

  subgraph M5["⑤ 数据审核"]
    A5["/audit · /api/audit/*"]
  end

  subgraph M6["⑥ 数据管理"]
    A6["/manage · /api/manage/*"]
  end

  subgraph M7["⑦ 任务与工作流"]
    A7["/tasks · /api/tasks* · /api/workflow/tasks*"]
  end

  subgraph M8["⑧ 消息与通知"]
    A8["/messages · /api/messages/*"]
    A8b["/api/admin/message-* · announcements · delivery-log"]
  end

  subgraph M9["⑨ 用户管理"]
    A9["/users · /api/users*"]
  end

  subgraph M10["⑩ 安全审计（账号维度）"]
    A10["/api/audit/login-logs · user-actions · security-export"]
    A10b["/api/admin/sessions/recent"]
  end

  M2 --> M1
  M1 --> M3
  M3 --> M4
  M4 --> M5
  M5 --> M6
  M6 -. 查询同一批录制数据 .-> M3
  M7 -. 驱动处理与分配 .-> M4
  M8 -. 任务/截止提醒 .-> M7
  M9 -. 账号与角色 .-> M2
  M10 -. 审计留痕 .-> M9
```

---

## 图 2：核心业务链路（录制生命周期）

```mermaid
flowchart LR
  UP["上传/采集\nupload · crawl"] --> PR["处理\nprocess · bagel"]
  PR --> RV["待审核\npending_review"]
  RV --> AD["审核\npass / reject / self-fix"]
  AD --> DM["数据管理\n检索 · 导出 · 数据集"]
  DM --> EX["导出/归档\nexport · batch-dl"]

  TW["任务工作流\ntasks / workflow"] -. 分配与进度 .-> PR
  TW -. 分配与进度 .-> RV
  MS["消息\ninbox / admin"] -. 通知 .-> TW
```

---

## 图 3：思维导图（总览）

```mermaid
mindmap
  root((MAPS / MMDAPS))
    门户总览
      仪表板统计
    认证与个人
      登录会话
      资料与改密
    数据采集
      本地上传
      流式上传
      互联网采集API
    数据处理
      处理工作台
      转写与手工保存
      BAGEL异步与Redis
    数据审核
      待审队列
      通过打回自修
    数据管理
      列表筛选
      预览下载
      自定义数据集
      导出
    任务工作流
      任务统计
      申领提交复核归档
    消息通知
      站内收件箱
      模板渠道公告
    用户管理
      CRUD与权限
    安全审计
      登录与操作日志
```

---

## 图 4：实现映射（页面 ↔ 服务层）

```mermaid
flowchart TB
  subgraph UI["templates/ 页面"]
    T1[index · upload · process · audit · manage · tasks · messages · users · profile]
  end

  subgraph APP["app.py 路由与 API"]
    R["Flask routes"]
  end

  subgraph SVC["services/"]
    D[data_management]
    T[task_workflow]
    G[messaging]
    B[bagel_queue]
    X[auth_security]
  end

  subgraph DATA["models.py · MySQL/SQLite"]
    DB[(ORM 模型)]
  end

  T1 --> R
  R --> D
  R --> T
  R --> G
  R --> B
  R --> X
  D --> DB
  T --> DB
  G --> DB
  B -. Redis .-> R
```

---

## 使用说明

- **单文件导出 PNG/SVG**：打开 [mermaid.live](https://mermaid.live)，粘贴某一图中的代码块内容，使用菜单导出。  
- 与分层架构说明并列阅读：**[ARCHITECTURE_LAYERS.md](./ARCHITECTURE_LAYERS.md)**。
