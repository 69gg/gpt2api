# gpt2api

OpenAI/Anthropic 兼容 API，后端接入 ChatGPT Web Chat (`chatgpt.com`)。

支持：
- `/v1/chat/completions` (OpenAI 聊天)
- `/v1/responses` (OpenAI Responses)
- `/v1/messages` (Anthropic Messages)
- `/v1/images/generations` (图片生成，支持图片代理)
- `/v1/models`
- `/admin/*` Token 池管理
- 单 token 代理配置（`http`/`socks5`/`socks5h`）
- 分布式注册脚本自动推送 token

## 依赖

用 [uv](https://github.com/astral-sh/uv) 管理依赖：

```bash
uv sync
```

## 快速开始

### 1. 注册账号并获取 Platform Token

需要自建/接入一个 Cloudflare Worker 邮箱服务，然后运行注册脚本：

```bash
uv run python app/reg_web.py \
  --cf-url https://your-cf-email-worker.workers.dev \
  --cf-auth <x-custom-auth密码> \
  --cf-admin-auth <x-admin-auth密码（可选，走admin API）> \
  --cf-domain your-domain.com \
  --proxy http://127.0.0.1:7890
```

注册成功后获取的是 **Platform OAuth token**，直接保存到 `web_token/` 目录。该 token 可直接用于 `chatgpt.com` 的 Web Chat API（`f/conversation` 端点），**无需额外的 webchat token 获取步骤**。

### 2. 配置服务

复制示例配置并编辑：

```bash
cp config.yaml.example config.yaml
```

编辑 `config.yaml`：

```yaml
server:
  host: "0.0.0.0"
  port: 8000
  api_key: "sk-gpt2api"            # API 访问密钥
  admin_key: "admin-gpt2api"       # 管理端点密钥
  deployment_url: ""               # gpt2api 部署 URL（用于图片代理，如 https://your-domain.com）

register:
  cf_url: ""                       # Cloudflare Worker 邮箱后端 URL
  cf_auth: ""                      # 站点访问密码
  cf_admin_auth: ""                # 管理密码
  cf_domain: ""                    # 邮箱域名
  proxy: ""                        # 注册用代理
  auto_register: false             # 自动注册（token 不足时）
  min_tokens: 3                    # 最少保持的 token 数

token:
  refresh_interval_hours: 2        # 自动刷新过期 token
  dead_retain_hours: 24            # 死 token 保留时间
  cooling_reset_hours: 24          # 冷却重置时间
  fail_threshold: 5                # 失败多少次标记为 dead
  load_balance: "round-robin"      # 负载策略: round-robin / random / least-used

chatgpt:
  proxy: ""                        # 全局代理（每个 token 的 proxy 字段优先）
  sse_timeout: 120
  pow_max_iter: 500000
  image_download_timeout: 60
  turnstile_solver_url: ""         # 外部 Turnstile 求解器（可选）

models:                             # 可用模型列表
  - id: "gpt-5.3-codex"
  - id: "gpt-5.2-codex"
  - id: "o4-mini"
  - id: "gpt-image-2"
```

所有字段都支持用环境变量覆盖，命名规则 `GPT2API_<section>_<key>`，例如：

```bash
export GPT2API_SERVER_API_KEY=sk-xxxx
export GPT2API_CHATGPT_PROXY=http://127.0.0.1:7890
export GPT2API_TOKEN_REFRESH_INTERVAL_HOURS=1
```

### 3. 启动服务

```bash
# 默认配置
uv run python main.py

# 指定端口
uv run python main.py --port 8000

# 生产（多 worker，暂不支持 reload）
uv run uvicorn "app.server:create_app" --factory --host 0.0.0.0 --port 8000
```

## API 使用

### Chat Completions

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-gpt2api" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role":"user","content":"你好"}],
    "stream": true
  }'
```

### Anthropic Messages

```bash
curl http://localhost:8000/v1/messages \
  -H "Authorization: Bearer sk-gpt2api" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-20250514",
    "messages": [{"role":"user","content":"你好"}],
    "max_tokens": 4096,
    "stream": true
  }'
```

### Image Generation

```bash
curl http://localhost:8000/v1/images/generations \
  -H "Authorization: Bearer sk-gpt2api" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-image-2",
    "prompt": "a cat in space"
  }'
```

## Token 格式

每个 token 保存为独立的 JSON 文件在 `web_token/` 目录下。token 文件支持 `proxy` 字段设置**单 token 代理**，优先于全局代理：

```json
{
  "email": "tmpo...@hwqs.dadongbei.asia",
  "proxy": "socks5://user:pass@host:port",
  "...": "..."
}
```

支持的代理类型：`http://`、`socks5://`、`socks5h://`。

## 图片代理

当 `server.deployment_url` 配置后，图片生成返回的 URL 会自动替换为 gpt2api 部署下的代理链接：

```yaml
server:
  deployment_url: "https://your-domain.com"  # 或 http://localhost:8000
```

代理端点 `/p/img/{base64url_estuary_url}` 会带正确的 Referer 和 auth 头向 estuary 获取图片，绕过防盗链。

## 分布式注册

`reg_distributed.py` 可独立运行，注册完成后自动推送到多个 gpt2api 实例：

```bash
# 注册 5 个账号
uv run python reg_distributed.py \
  --cf-url https://your-cf-email-worker.workers.dev \
  --cf-auth xxx \
  --instances "http://host1:8000,http://host2:8000" \
  --admin-key admin-gpt2api \
  --count 5 \
  --save-local

# 无限注册模式（Ctrl+C 优雅退出）
uv run python reg_distributed.py \
  --cf-url https://your-cf-email-worker.workers.dev \
  --cf-auth xxx \
  --instances "http://host1:8000" \
  --infinite --interval 30
```

参数说明：
- `--instances`: 逗号分隔的 gpt2api 部署地址
- `--admin-key`: 管理端点密钥（用于 POST /admin/tokens）
- `--count`: 注册数量（0 或 `--infinite` 为无限模式）
- `--infinite`: 无限注册，持续运行直到 Ctrl+C
- `--interval`: 两次注册间隔秒数（默认 10）
- `--retry`: 单次注册最大重试次数（默认 3）
- `--retry-delay`: 注册重试基础延迟秒数（默认 10，线性递增）
- `--push-retry`: 推送到实例最大重试次数（默认 3）
- `--push-retry-delay`: 推送重试基础延迟秒数（默认 5）
- `--save-local`: 是否同时保存到本地 `web_token/` 目录

## 管理端点

所有管理端点需要 `X-Admin-Key` 头：

```bash
# 查看 Token 池状态
curl http://localhost:8000/admin/tokens \
  -H "X-Admin-Key: admin-gpt2api"

# 手动刷新过期 token
curl -X POST http://localhost:8000/admin/tokens/refresh \
  -H "X-Admin-Key: admin-gpt2api"

# 手动注册新账号（需要配置 cf_url）
curl -X POST http://localhost:8000/admin/register \
  -H "X-Admin-Key: admin-gpt2api" \
  -H "Content-Type: application/json" \
  -d '{"cf_url":"https://your-worker.workers.dev","cf_auth":"xxx"}'

# 添加 token
curl -X POST http://localhost:8000/admin/tokens \
  -H "X-Admin-Key: admin-gpt2api" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "user@example.com",
    "access_token": "eyJ...",
    "refresh_token": "rt_...",
    "proxy": "http://127.0.0.1:7890",
    "status": "active"
  }'

# 禁用/启用 token
curl -X POST http://localhost:8000/admin/tokens/email@example.com/disable \
  -H "X-Admin-Key: admin-gpt2api"

# 删除 token
curl -X DELETE http://localhost:8000/admin/tokens/email@example.com \
  -H "X-Admin-Key: admin-gpt2api"
```

## 目录结构

```
gpt2api/
├── main.py                 # 启动入口
├── config.yaml.example     # 示例配置（复制为 config.yaml 使用）
├── pyproject.toml          # uv 依赖
├── web_token/              # 自动生成的 token 文件（.gitignore）
├── reg_distributed.py      # 分布式注册脚本（独立运行，自动推送到实例）
├── app/
│   ├── server.py           # FastAPI 应用 + 后台任务
│   ├── config.py           # 配置管理
│   ├── auth.py             # 认证中间件
│   ├── models.py           # 模型列表定义
│   ├── token_manager.py    # Token 池管理
│   ├── reg_web.py          # 自动注册脚本
│   ├── chatgpt/
│   │   ├── client.py       # Web Chat 客户端 (f/conversation)
│   │   ├── sentinel.py     # Sentinel + POW 解算
│   │   ├── retry.py        # 重试工具
│   │   ├── sse.py          # SSE 流解析
│   │   ├── image.py        # 图片生成
│   │   └── turnstile.py    # Turnstile 求解器（外部服务/VM）
│   ├── adapters/
│   │   ├── openai_chat.py  # /v1/chat/completions
│   │   ├── openai_resp.py  # /v1/responses
│   │   ├── openai_image.py # /v1/images/generations
│   │   └── anthropic.py    # /v1/messages
│   └── routes/
│       ├── chat.py
│       ├── response.py
│       ├── messages.py
│       ├── models.py
│       ├── admin.py
│       └── proxy.py
```

## Token 生命周期

服务启动后自动启动 5 个后台任务：

1. **Token 刷新**（默认每 2 小时）：用 `refresh_token` 换取新的 `access_token`
2. **冷却恢复**（每 10 分钟）：配额耗尽的 token 等待冷却期后恢复 active
3. **新 Token 扫描**（每 30 秒）：检测 `web_token/` 目录新增的文件
4. **死 Token 清理**（每小时）：删除超过保留期的 dead token
5. **自动注册**（每 60 秒检查）：当 `auto_register: true` 且 active token 数 < `min_tokens` 时自动注册新账号

## 注意事项

- **web_token/** 目录包含敏感凭证，已加入 `.gitignore`，请勿提交到仓库
- `chatgpt.com` 的反爬策略变化频繁，`app/chatgpt/` 下的实现需要持续维护
- **Platform OAuth token 可直接用于 `chatgpt.com` 的 Web Chat API**，无需单独获取 webchat token；服务端通过 `SentinelClient` 自动处理 chat-requirements + POW
- SSE 流已适配 `chatgpt.com` 最新的 `delta` 事件 + JSON Patch 增量格式
- 免费账号有配额限制，建议用多个 token 轮询分担负载
