# Gemini CLI → Claude Proxy

用 Gemini CLI 的交互体验，跑 Claude 的模型。

## 原理

```
Gemini CLI ──(Gemini API)──▶ 本地 proxy :8888 ──(Anthropic Messages API)──▶ Vertex AI Claude
```

proxy 做两件事：
1. Gemini API 请求格式 → Anthropic Messages API 格式
2. 用本机 ADC (Application Default Credentials) 自动认证 Vertex AI

## 前置条件

- Python 3.9+
- `google-auth` (`pip install google-auth requests`)
- Gemini CLI (`npm install -g @google/gemini-cli`)
- GCP 认证（`gcloud auth application-default login` 或 GCE 默认 SA）
- SA 需要 `roles/aiplatform.user` 权限

## 快速开始

```bash
# 1. 启动 proxy（后台）
python3 ~/CloseCrab/scripts/gemini-claude-proxy.py &

# 2. 启动 Gemini CLI（接 Claude Opus）
GOOGLE_GEMINI_BASE_URL="http://127.0.0.1:8888" \
GEMINI_API_KEY=dummy \
gemini -m gemini-2.5-pro
```

`GEMINI_API_KEY` 随便填，认证走本机 ADC。

## 模型映射

| Gemini CLI `-m` 参数 | 实际调用的 Claude 模型 |
|---|---|
| `gemini-2.5-pro` | `claude-opus-4-6` |
| `gemini-2.5-flash` | `claude-sonnet-4-6` |
| 其他 | `claude-sonnet-4-6` (默认) |

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VERTEX_PROJECT` | `chris-pgp-host` | GCP 项目 |
| `VERTEX_LOCATION` | `global` | Vertex AI 区域 |
| `PROXY_PORT` | `8888` | proxy 监听端口 |

## 推荐 alias

```bash
# 加到 ~/.zshrc 或 ~/.bashrc
alias gclaude='GOOGLE_GEMINI_BASE_URL="http://127.0.0.1:8888" GEMINI_API_KEY=x gemini'
```

然后直接用：

```bash
gclaude -m gemini-2.5-pro          # Claude Opus
gclaude -m gemini-2.5-flash        # Claude Sonnet
gclaude -m gemini-2.5-pro --yolo   # YOLO 模式
```

## Gemini CLI 配置

首次使用需要设置 auth 类型为 `gemini-api-key`：

```bash
# ~/.gemini/settings.json
{"security":{"auth":{"selectedType":"gemini-api-key"}}}
```

或者首次启动 Gemini CLI 时在交互界面选择 "API Key" 认证方式。

## 日志

proxy 日志输出到 stderr：

```
[proxy] Auth warmup... OK
Proxy on :8888 (chris-pgp-host/global)
[proxy] gemini-2.5-pro → claude-opus-4-6 (stream) msgs=2
[proxy] ✓ 156 chars (7385+42 tok)
```

## 已知限制

- 不支持图片/多模态输入（只转发文本）
- 不支持 function calling / tool use 转换
- Claude 不接受 `temperature` + `top_p` 同时设置，proxy 自动丢弃 `top_p`
- 非真正流式：proxy 等 Claude 完整回复后一次性返回（Gemini CLI 感知不到区别）
