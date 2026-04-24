# gpt2api

OpenAI/Anthropic 兼容 API，后端接入 ChatGPT Web Chat (`chatgpt.com`)。

支持：
- `/v1/chat/completions` (OpenAI 聊天)
- `/v1/responses` (OpenAI Responses)
- `/v1/messages` (Anthropic Messages)
- `/v1/images/generations` (图片生成)
- `/v1/models`
- `/admin/*` Token 池管理

## 依赖

用 [uv](https://github.com/astral-sh/uv) 管理依赖：

```bash
uv sync
```

## 快速开始

### 1. 注册账号并获取 Web Token

需要自建/接入一个 Cloudflare Worker 邮箱服务，然后运行注册脚本：

```bash
uv run python app/reg_web.py \
  --cf-url https://your-cf-email-worker.workers.dev \
  --cf-auth <x-custom-auth密码> \
  --cf-admin-auth <x-admin-auth密码（可选，走admin API）> \
  --cf-domain your-domain.com \
  --proxy http://127.0.0.1:7890
```

注册成功后 token 保存到 `web_token/` 目录。

### 2. 配置服务

编辑 `config.yaml`：

```yaml
server:
  host: "0.0.0.0"
  port: 8000
  api_key: "sk-gpt2api"      # API 访问密钥
  admin_key: "admin-gpt2api" # 管理端点密钥

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
  proxy: ""                        # 全局代理
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
├── config.yaml             # 配置文件
├── pyproject.toml          # uv 依赖
├── web_token/              # 自动生成的 token 文件（.gitignore）
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
│       └── admin.py
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
- Turnstile 求解器目前支持外部服务和 Capsolver；纯 Python VM 求解器待后续实现
- 免费账号有配额限制，建议用多个 token 轮询分担负载
