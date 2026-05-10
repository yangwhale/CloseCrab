---
name: mcp-proxy
description: gLinux MCP Proxy All-in-one 部署和维护。将 Google 内部 MCP servers（coding、bugged、workspace 等）聚合到单端口，通过 SSH tunnel 暴露给远程 VM 上的 Claude Code / Kilo worker。当遇到以下情况时使用：(1) 新机器需要配置 MCP proxy (2) MCP 工具调用报 schema 错误或 anyOf 兼容性问题 (3) 需要添加新的内部 MCP server (4) SSH tunnel 断开或 mcp-proxy 服务异常 (5) 用户说"装 MCP proxy"、"MCP 不通"。关键词："mcp-proxy"、"coding_server"、"bugged"、"internal MCP"、"anyOf"、"schema sanitizer"。
---

# mcp-proxy

将 Google 内部 MCP servers 聚合到单端口（18090），通过 SSH reverse tunnel 暴露给远程 VM。

## 架构

```
gLinux (tiemu)                          Remote VM (cc-tw)
┌─────────────────────┐                ┌──────────────────┐
│ coding_server.par   │                │                  │
│ bugged.par          │──► mcp-proxy ──┤── SSH tunnel ────┤──► localhost:18090
│ workspace MCP       │    (Go, :18090)│                  │    ↑
│ chrome-devtools-mcp │                │                  │  Claude/Kilo
│ c2xprof.par         │                │                  │  reads from here
└─────────────────────┘                └──────────────────┘
```

## 组件

### mcp-proxy (Go binary)
- 安装：`go install github.com/nicholasgasior/mcp-proxy@latest`
- 配置：`~/.config/mcp-proxy/config.json`
- systemd unit：`~/.config/systemd/user/mcp-proxy.service`

### coding-server-wrapper.py
`coding_server.par` 的 JSON Schema 包含 `anyOf: [{}]`（空 object），Vertex AI 和 Kilo 拒绝空 schema。Wrapper 拦截 `tools/list` 响应，将 `{}` 替换为 `{"type": "string"}`。

安装位置：`~/.local/bin/coding-server-wrapper.py`

在 mcp-proxy config.json 中配置：
```json
"coding": {
  "command": "python3",
  "args": ["/usr/local/google/home/<user>/.local/bin/coding-server-wrapper.py"]
}
```

### SSH reverse tunnel
```bash
ssh -R 18090:localhost:18090 cc-tw -N
```
用 autossh 或 systemd 保活。

## 安装步骤

1. 在 gLinux 安装 mcp-proxy：
```bash
go install github.com/nicholasgasior/mcp-proxy@latest
```

2. 部署 wrapper 脚本：
```bash
cp scripts/coding-server-wrapper.py ~/.local/bin/
chmod +x ~/.local/bin/coding-server-wrapper.py
```

3. 创建 mcp-proxy 配置（`~/.config/mcp-proxy/config.json`），参考 `references/config-example.json`

4. 启动 mcp-proxy systemd 服务：
```bash
systemctl --user enable --now mcp-proxy
```

5. 建立 SSH tunnel：
```bash
autossh -M 0 -f -N -R 18090:localhost:18090 cc-tw
```

## 故障排查

- **MCP 不通**：检查 `systemctl --user status mcp-proxy` 和 SSH tunnel
- **coding 工具报 schema 错误**：确认 config.json 中 coding 用的是 wrapper 而非直接调用 .par
- **LOAS2 凭证过期**：内部 MCP 的 .par 文件需要有效的 LOAS2 cert，运行 `gcert` 刷新
- **新增 MCP server**：在 config.json 添加条目，`systemctl --user restart mcp-proxy`
