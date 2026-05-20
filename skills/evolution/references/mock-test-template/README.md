# mock-test-template

Round 3 evolution loop receipt 模板 — 验证 channel inbox callback fast-path
invariant 跨平台一致。

## 文件

| 文件 | 用途 |
|------|------|
| `test_inbox_callback_invariant.py` | 三 channel (feishu/discord/dingtalk) 参数化 mock test，断言 `_make_input_callback(is_inbox=True)` 走 fast-path 不阻塞 |

## 跑法

不进主 CI，evolution loop 跑时 ad-hoc 调：

```bash
cd ~/CloseCrab
pip install -q pytest pytest-asyncio   # 一次性 dependency
python3 -m pytest skills/evolution/references/mock-test-template/test_inbox_callback_invariant.py -v
```

## Pre/Post-patch matrix（Round 3 evolution loop receipts）

| Channel | Pre-patch | Post-patch | Patch commit |
|---------|-----------|------------|--------------|
| feishu | ✗ FAIL (无 is_inbox 参数) | ✓ PASS | 361a38f (Round 2) |
| discord | ✗ FAIL (无 is_inbox 参数) | ✓ PASS | Round 3 |
| dingtalk | ✗ FAIL (inline closure, 无 helper) | ✓ PASS | Round 3 |

## Pattern

测试模板可复用到任何 channel callback / worker handler / 跨平台 invariant
验证场景。核心 pattern：

1. 用 `Channel.__new__(Channel)` 跳 `__init__`，只塞测试用的最小 state
2. AsyncMock / MagicMock 网络副作用
3. `asyncio.wait_for(cb, timeout=1.0)` 限制单测时间
4. 同时验证 fast-path（is_inbox=True）+ regression path（is_inbox=False 不退化）

## 沉淀关系

- 跟 `evolution/references/control-request-fastpath.md` 互补：前者是 design
  pattern 文档，本目录是可执行 receipt
- 跟 `feedback_production-vs-source-channel-divergence` (GBrain) 互补：那条
  解释为什么 discord/dingtalk channel 0 production 还要 patch（防御性）
