# Control Request Fast-Path（inbox 派活的 ExitPlanMode / AskUserQuestion）

> Round 1 case-3 实测教训：worker 触发 ExitPlanMode + AskUserQuestion，但消息源是 bot-to-bot inbox，没有真用户在飞书 UI 上答卡片。  
> Channel callback 等 5 min 超时返回 "继续" → worker 把"继续"当 plan modification → 又触发新一轮 control_request → 5 轮累积 25 min → 命中 BotCore 1800s user lock timeout → 强杀 → 后续 case CancelledError。

## 反模式：让 user-facing 路径处理 bot-to-bot 消息

任何 channel 实现的 `on_input_needed` callback，**默认设计是给真人答**：
- 显示卡片 / 按钮
- 等待用户点击或回复
- 超时回 "继续" 当 graceful fallback

inbox 路径用同一个 callback 会**复合放大失败**：
- 没有真人 → 等满超时
- 每次返回 "继续" → 不构成 plan/question 的有效答案 → worker 重新发起
- N 次累积 → 触发上游硬超时

## 推荐 fast-path 模板

每个 channel 的 `_make_input_callback` 都该有 `is_inbox` 参数：

```python
def _make_input_callback(self, ctx, user_key, is_inbox: bool = False):
    async def on_input_needed(info: dict) -> Optional[str]:
        tool = info.get("tool", "")
        inp = info.get("input", {})
        
        # Inbox 来源没真人答 → 立即 auto-approve
        if is_inbox:
            if tool == "ExitPlanMode":
                return "approved"           # 让 worker 退出 plan mode
            if tool == "AskUserQuestion":
                # 拿每个 question 的第一个 option label
                qs = inp.get("questions", []) or []
                return "\n".join(
                    (q.get("options") or [{"label": "继续"}])[0].get("label", "继续")
                    for q in qs
                ) or "继续"
            return "继续"
        
        # 真用户路径（原 5 min 等待逻辑）
        ...
    return on_input_needed
```

调用方根据消息来源传 `is_inbox`：

```python
metadata = {
    ...
    "on_input_needed": self._make_input_callback(chat_id, user_key, is_inbox=bool(inbox_from)),
}
```

## 验证结果

Round 1 case-3 (broken): 28 min, 46 steps, 强杀  
Round 2 case-3 (patched): **50.5 s, 11 steps, done** — **34× 提速**

## 三平台同步检查

每次改一个 channel 的 inbox fast-path，**必须**同步检查另外两个：

- `discord.py:_make_input_callback` — 有同样的 5 min 超时？
- `dingtalk.py:_make_input_callback` — 同上？

Discord 的 ExitPlanMode/AskUserQuestion 走 button interaction，可能不影响；钉钉是纯文本回，行为可能跟飞书一样。**Round 3 候选 case: 验证 Discord/钉钉 inbox 派 ExitPlanMode 是否也死锁。**
