---
name: chrome-browser
description: 通过 Chrome DevTools MCP 访问需要内部权限的网页应用（Moma、Gmail、Google Chat、Calendar、go/ links 等）。当用户说"看邮件"、"查 Moma"、"搜人"、"看 Chat"、"打开浏览器"、"go/xxx"、"看日历"、"chrome"、"内部网站"等关键词时触发。优先使用专用 MCP/skill，Chrome 浏览器作为兜底。
---

# Chrome Browser — 内部网页访问

通过 Chrome DevTools MCP 操控用户浏览器，访问需要 Google 内部权限认证的 web 应用。

**原则**：专用 MCP/skill 优先（如 bugged skill 访问 Buganizer/Code Search，workspace MCP 访问 Docs/Sheets）。Chrome 浏览器是兜底方案，用于没有专用工具覆盖的场景。

## 前置检查：Chrome Debug 模式

操作前先调用 `list_pages`。如果失败或无法连接，说明 Chrome 没有以 debug 模式启动。

**告诉用户执行以下步骤：**

1. 完全关闭 Chrome（包括后台进程）
2. 用 debug 模式重启：

```bash
# gLinux
google-chrome --remote-debugging-port=9222 &

# macOS
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 &

# 如果 Chrome 关不干净，先杀进程
pkill -f google-chrome && sleep 2
google-chrome --remote-debugging-port=9222 &
```

3. 再次调用 `list_pages` 确认连接成功

**注意**：`--remote-debugging-port=9222` 是 Chrome DevTools MCP 的默认端口。如果 MCP 配置了其他端口，以配置为准。

## 操作 Pattern

### 基本流程

```
1. list_pages          → 查看当前打开的页面
2. navigate_page       → 导航到目标 URL
3. take_snapshot       → 获取页面 a11y tree（优先于 screenshot）
4. fill / click / press_key → 交互操作
5. take_snapshot       → 确认结果
```

### 处理大页面

snapshot 过大时用 `filePath` 参数存到文件，再用 Grep 提取关键信息：

```
take_snapshot(filePath="/tmp/page.txt")
→ Grep 搜索关键字段
→ 或 Bash + python3 解析特定行范围
```

### 搜索类操作

1. 找到搜索框的 uid（通过 snapshot）
2. `fill(uid, value)` 输入搜索词
3. `press_key("Enter")` 或 `click` 搜索按钮
4. `take_snapshot` 读取结果

## 支持的内部工具

### Moma — 人员/团队搜索

- URL: `https://moma.corp.google.com/`
- 人员搜索: `https://moma.corp.google.com/search?q={name}&hq=type:people`
- 个人 profile: `https://moma.corp.google.com/person/{ldap}`
- 用途: 搜人、查 org chart、看 report line、团队信息
- 搜索框通常是 combobox，uid 在 `search "search bar"` 区域内

### Gmail — 邮件

- URL: `https://mail.google.com/`
- 收件箱: 默认打开 inbox，snapshot 包含邮件列表（发件人、主题、时间）
- 搜索: 使用页面顶部搜索框
- 读邮件: click 邮件行进入详情
- 注意: 页面 snapshot 通常很大，用 filePath 存文件再 grep

### Google Chat — 即时通讯

- URL: `https://chat.google.com/`
- 侧栏: DM 列表、Spaces 列表，显示未读状态
- 发消息: click 进入对话 → fill 输入框 → press Enter
- 看 thread: click thread reply 按钮展开
- 注意: 页面用 split pane，左侧导航 + 右侧内容

### Calendar — 日历

- URL: `https://calendar.google.com/`
- 查看日程、会议详情
- 导航: 可以切换日/周/月视图

### go/ Links

go/ 短链接是 Google 内部常用的 URL 缩写。直接导航：

```
navigate_page(type="url", url="http://go/rainmaker")
navigate_page(type="url", url="http://go/eat")
navigate_page(type="url", url="http://go/moma")
navigate_page(type="url", url="http://go/meet-rp")
```

go link 会 redirect 到实际页面，等 redirect 完成后再 `take_snapshot`。如果 navigate 超时，可能是 redirect 链较长，重试或直接用 `take_snapshot` 看当前状态。

### 其他 Corp 站点

任何 `*.corp.google.com` 或需要内部认证的站点都可以通过 Chrome 访问：
- Googler News: `gn.corp.google.com`
- Campus Maps: `campusmaps.googleplex.com`
- 内部 dashboards、sites 等

## 注意事项

- **认证**: Chrome 已登录用户的 Google 账号，直接继承 session，无需额外认证
- **snapshot 优先于 screenshot**: snapshot 返回结构化的 a11y tree，可精确定位元素 uid 进行交互；screenshot 只是图片，无法交互
- **超时处理**: 内部站点可能加载慢，navigate_page 超时时先 take_snapshot 看当前状态
- **隐私**: 邮件、聊天内容属于敏感信息，只展示用户明确要求看的内容
