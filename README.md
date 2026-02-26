# WeiXinAgent

本项目是一个本地部署的微信公众号智能客服服务：
- 微信公众号回调：`/wechat`
- 本地大模型（Ollama）回答
- 本地知识库检索（支持文本/PDF/Word/图片）
- 支持“本地文件夹批量入库 + 增量同步”

## 1. 日常启动（推荐）

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_app.ps1
```

或直接双击根目录脚本：
`start_agent.bat`（一键启动应用 + named tunnel + 健康检查）

启动脚本会自动：
1. 启动 `ollama serve`（若未运行）
2. 启动 `uvicorn app.main:app`（若未运行）
3. 创建知识库目录（默认 `.\kb_source`）
4. 健康检查 `http://127.0.0.1:8000/healthz`

## 2. 长期固定 URL（不再频繁改公众号后台）

你现在用的是 Quick Tunnel，域名会变。  
要固定 URL，使用 **Cloudflare Named Tunnel + 你自己的域名**。

### 2.1 一次性配置

假设你要用的域名是 `wxbot.example.com`：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_named_tunnel.ps1 -Hostname wxbot.example.com -TunnelName weixin-agent
```

脚本会依次执行：
1. `cloudflared tunnel login`（会打开浏览器登录 Cloudflare）
2. `cloudflared tunnel create weixin-agent`
3. `cloudflared tunnel route dns weixin-agent wxbot.example.com`

### 2.2 日常运行固定隧道

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_named_tunnel.ps1 -TunnelName weixin-agent
```

然后公众号后台 URL 固定填：

`https://wxbot.example.com/wechat`

只要你的本机服务和 named tunnel 在跑，就不需要再改 URL。

## 3. 本地文件夹批量知识库（免逐个上传）

### 3.1 设置知识库目录

在 `.env` 中设置（可用绝对路径）：

```ini
KB_SOURCE_DIR=E:\PyProject\WeiXinAgent\kb_source
KB_AUTO_SYNC_ON_START=1
KB_SYNC_INTERVAL_SEC=0
```

说明：
- `KB_AUTO_SYNC_ON_START=1`：应用启动时自动同步一次
- `KB_SYNC_INTERVAL_SEC=0`：关闭定时同步（改成 `60` 表示每 60 秒自动增量同步）

### 3.2 使用方式

1. 把文件直接复制到 `KB_SOURCE_DIR`（可多层目录）
2. 触发同步（任选一种）

手动触发：
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\sync_kb_folder.ps1
```

或者重启应用（因为有 `KB_AUTO_SYNC_ON_START=1` 会自动同步）。

### 3.3 支持格式

`txt/md/csv/json/pdf/docx/png/jpg/jpeg/bmp/webp`

## 4. 单文件上传（仍可用）

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\upload_kb.ps1 -FilePath "E:\docs\faq.txt"
```

## 5. 常用接口

- `GET /healthz`
- `GET|POST /wechat`
- `POST /kb/upload`（管理员）
- `POST /kb/sync`（管理员，触发目录同步）
- `GET /kb/sync/status`（管理员）
- `GET /kb/sources`（管理员）
- `GET /kb/query?q=...`
- `GET /wechat/access-token`（管理员）

## 6. 管理员鉴权

管理员接口都需要请求头：

`X-Admin-Token: <你的 ADMIN_TOKEN>`

## 7. 关键配置项（.env）

```ini
WECHAT_TOKEN=...
WECHAT_APP_ID=...
WECHAT_APP_SECRET=...
WECHAT_ENCODING_AES_KEY=
WECHAT_REPLY_TIMEOUT_SEC=2.5

ADMIN_TOKEN=...

OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_CHAT_MODEL=qwen3:14b
OLLAMA_EMBED_MODEL=qwen3:14b
OLLAMA_VISION_MODEL=

KB_DB_PATH=./data/kb.sqlite3
KB_SOURCE_DIR=./kb_source
KB_AUTO_SYNC_ON_START=1
KB_SYNC_INTERVAL_SEC=0
MAX_CHUNK_CHARS=500
TOP_K=4
```

说明：`qwen3:14b` 不支持 embeddings，系统会自动降级为关键词检索。
