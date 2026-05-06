# Live2D 情绪可视化系统

> AI 角色通过 Live2D 实时展示 Gemini TTS 情绪标签对应的面部表情。本文档是完整的设计和实现参考，为集成到 LiveKit 语音助手前端做准备。

**线上 Demo**: <https://cc.higcp.com/pages/ai-live2d-v2-20260506.html>
**角色**: Jin Natori（男性，Cubism 4）
**更新**: 2026-05-06

### 项目内文件

| 文件 | 用途 |
|---|---|
| `closecrab/voice/web/live2d-emotion.js` | 可复用的 JS 模块（`Live2DEmotion` 类） |
| `closecrab/voice/web/live2d-demo.html` | 引用本地模块的工作 Demo |
| `assets/live2d/natori/` | Natori 模型资源（自托管，不依赖外部 CDN） |
| `scripts/download-live2d-models.sh` | 模型下载脚本（支持下载其他角色） |

---

## 1. 架构

```
Gemini TTS Response
  │
  ├─ 文本流中包含 emotion tag，如 "[excited] 太棒了！"
  │
  ▼
前端解析层
  │
  ├─ 提取 [tag] → 查 EMOTION_MAP → 得到 expression name
  │
  ▼
Live2D 渲染层
  │
  ├─ model.expression(name)     → 切换面部表情
  ├─ model.motion('Idle')       → 播放待机动画
  ├─ wrapper.style.background   → 切换氛围背景
  └─ UI 状态更新               → 情绪图标/名称/标签
```

---

## 2. 技术栈与版本约束

### 依赖清单

| 库 | 版本 | CDN URL | 用途 |
|---|---|---|---|
| PixiJS | 6.5.10 | `https://cdn.jsdelivr.net/npm/pixi.js@6.5.10/dist/browser/pixi.min.js` | Canvas 渲染引擎 |
| Cubism Core | latest | `https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js` | Live2D WASM 运行时 |
| pixi-live2d-display | 0.4.0 | `https://cdn.jsdelivr.net/npm/pixi-live2d-display@0.4.0/dist/cubism4.min.js` | PixiJS ↔ Cubism 桥接 |

### 脚本加载顺序（严格）

```html
<!-- 1. PixiJS v6 — 必须最先加载 -->
<script src="https://cdn.jsdelivr.net/npm/pixi.js@6.5.10/dist/browser/pixi.min.js"></script>
<script>window.PIXI = PIXI;</script>
<!-- 2. Cubism 4 Core Runtime (WASM) -->
<script src="https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js"></script>
<!-- 3. pixi-live2d-display Cubism 4 build — 依赖上面两个 -->
<script src="https://cdn.jsdelivr.net/npm/pixi-live2d-display@0.4.0/dist/cubism4.min.js"></script>
```

> **`window.PIXI = PIXI` 不可省略**。pixi-live2d-display 在初始化时从 `window.PIXI` 读取 PixiJS 实例，PixiJS v6 ESM build 不会自动挂到 window 上。

### 版本兼容性矩阵

| pixi-live2d-display | PixiJS | Cubism SDK | moc3 version |
|---|---|---|---|
| 0.4.0 (cubism4 build) | v6.x | Cubism 2 / 3 / 4 | ≤ 3 (byte 5: `01` 或 `03`) |
| 0.4.0 (cubism2 build) | v6.x | Cubism 2 only | N/A (.moc 格式) |

### ⚠️ Cubism 5 不兼容

pixi-live2d-display 0.4.0 **无法加载 Cubism 5 模型**：

- Cubism 5 模型的 moc3 文件第 5 字节 = `06`（version 6）
- Cubism Core WASM 的 `Live2DCubismCore.Moc.fromArrayBuffer()` 返回 `null`
- pixi-live2d-display 抛出 `Unknown error`

验证方法：
```bash
od -A n -t x1 -j 4 -N 1 model.moc3
# 01 或 03 → Cubism 4 兼容 ✓
# 06       → Cubism 5 ✗
```

### 免费模型来源

Live2D 官方 CubismWebSamples 仓库提供免费模型（Free Material License）：

| Tag | Cubism 版本 | 可用模型 |
|---|---|---|
| `4-r.7` | Cubism 4 兼容 | Haru, Natori, Mark, Rice, Mao, Wanko 等 |
| `develop` (main) | Cubism 5 | 同名但已升级，**不兼容 0.4.0** |

模型 URL 模板：
```
https://raw.githubusercontent.com/Live2D/CubismWebSamples/{tag}/Samples/Resources/{Name}/{Name}.model3.json
```

当前使用：
```
https://raw.githubusercontent.com/Live2D/CubismWebSamples/4-r.7/Samples/Resources/Natori/Natori.model3.json
```

---

## 3. Emotion 映射系统

### 3.1 完整映射表

| Key | Icon | 中文 | Expression | Gemini TTS Tags | 背景渐变 |
|---|---|---|---|---|---|
| `calm` | 😌 | 平静 | `Normal` | `[calm]` `[casually]` `[whispers]` | teal → indigo → blue |
| `happy` | 😊 | 微笑 | `Smile` | `[happy]` `[cheerful]` | yellow → orange → cream |
| `excited` | 🤩 | 兴奋 | `exp_02` | `[excited]` `[amazed]` `[laughing]` | orange → pink → red |
| `angry` | 😡 | 生气 | `Angry` | `[angry]` `[scornful]` | pink → red → brown |
| `frustrated` | 😤 | 烦躁 | `exp_03` | `[frustrated]` `[serious]` `[sarcastic]` | brown → peach → gray |
| `surprised` | 😲 | 惊讶 | `Surprised` | `[surprised]` `[curious]` `[gasp]` `[urgent]` | pink → yellow → cream |
| `blushing` | 😳 | 害羞 | `Blushing` | `[empathetic]` | purple → pink → pink |
| `sad` | 😢 | 难过 | `Sad` | `[sad]` `[bored]` `[sighs]` `[crying]` | blue → indigo → gray |

### 3.2 Expression 参数详情

每个 `.exp3.json` 文件定义了一组 Cubism 参数的偏移值：

| Expression | 关键参数修改 | 视觉效果 |
|---|---|---|
| `Normal` | 全部默认值 | 自然平和 |
| `Smile` | EyeOpen=-1, EyeSmile=1, BrowForm=1 | 眯眼微笑 ^_^ |
| `exp_02` | EyeForm=1, MouthForm=1, TeethOn=1 | 露齿大笑 |
| `Angry` | EyeForm=-2, BrowAngle=-0.4, MouthForm=-2 | 皱眉怒视 |
| `exp_03` | BrowForm=-1, MouthForm=-2, MouthForm2=1 | 冷漠不耐烦 |
| `Surprised` | EyeOpen=0.3, EyeBallForm=-1, MouthForm=-3 | 瞪大双眼张嘴 |
| `Blushing` | Cheek=1, MouthForm=-2, MouthForm2=1 | 脸颊泛红 |
| `Sad` | EyeForm=-2, BrowForm=-1, MouthForm=-1 | 眼神黯淡 |

### 3.3 TTS Tag → Emotion 反向查找

在实际集成时，需要从 TTS 文本中提取 tag 并映射到 emotion key：

```javascript
const TAG_TO_EMOTION = {
    '[calm]': 'calm', '[casually]': 'calm', '[whispers]': 'calm',
    '[happy]': 'happy', '[cheerful]': 'happy',
    '[excited]': 'excited', '[amazed]': 'excited', '[laughing]': 'excited',
    '[angry]': 'angry', '[scornful]': 'angry',
    '[frustrated]': 'frustrated', '[serious]': 'frustrated', '[sarcastic]': 'frustrated',
    '[surprised]': 'surprised', '[curious]': 'surprised', '[gasp]': 'surprised', '[urgent]': 'surprised',
    '[empathetic]': 'blushing',
    '[sad]': 'sad', '[bored]': 'sad', '[sighs]': 'sad', '[crying]': 'sad',
};

function extractEmotion(text) {
    const match = text.match(/\[(\w+)\]/);
    if (match) {
        const tag = `[${match[1]}]`;
        return TAG_TO_EMOTION[tag] || 'calm';
    }
    return 'calm';
}
```

---

## 4. JavaScript 实现

### 4.1 Emotion 配置对象

```javascript
const EMOTIONS = {
    calm: {
        icon: '😌', name: '平静',
        ttsTag: '[calm]',
        ttsTags: ['[calm]', '[casually]', '[whispers]'],
        desc: 'Normal — 自然平和的默认表情',
        expression: 'Normal',
        motion: { group: 'Idle', index: undefined },
        bg: 'linear-gradient(160deg, #e0f2f1 0%, #e8eaf6 50%, #e3f2fd 100%)'
    },
    happy: {
        icon: '😊', name: '微笑',
        ttsTag: '[happy]',
        ttsTags: ['[happy]', '[cheerful]'],
        desc: 'Smile — 眯眼笑 ^_^',
        expression: 'Smile',
        motion: { group: 'Idle', index: undefined },
        bg: 'linear-gradient(160deg, #fef7e0 0%, #fff3e0 50%, #fff8e1 100%)'
    },
    excited: {
        icon: '🤩', name: '兴奋',
        ttsTag: '[excited]',
        ttsTags: ['[excited]', '[amazed]', '[laughing]'],
        desc: 'exp_02 — 露齿大笑，兴奋表情',
        expression: 'exp_02',
        motion: { group: 'Idle', index: undefined },
        bg: 'linear-gradient(160deg, #fff3e0 0%, #fce8e6 50%, #ffebee 100%)'
    },
    angry: {
        icon: '😡', name: '生气',
        ttsTag: '[angry]',
        ttsTags: ['[angry]', '[scornful]'],
        desc: 'Angry — 皱眉怒视，嘴角下拉',
        expression: 'Angry',
        motion: { group: 'Idle', index: undefined },
        bg: 'linear-gradient(160deg, #fce8e6 0%, #ffebee 50%, #fbe9e7 100%)'
    },
    frustrated: {
        icon: '😤', name: '烦躁',
        ttsTag: '[frustrated]',
        ttsTags: ['[frustrated]', '[serious]', '[sarcastic]'],
        desc: 'exp_03 — 不耐烦，冷漠表情',
        expression: 'exp_03',
        motion: { group: 'Idle', index: undefined },
        bg: 'linear-gradient(160deg, #efebe9 0%, #fbe9e7 50%, #f5f5f5 100%)'
    },
    surprised: {
        icon: '😲', name: '惊讶',
        ttsTag: '[surprised]',
        ttsTags: ['[surprised]', '[curious]', '[gasp]', '[urgent]'],
        desc: 'Surprised — 瞪大双眼，张嘴惊讶',
        expression: 'Surprised',
        motion: { group: 'Idle', index: undefined },
        bg: 'linear-gradient(160deg, #fce8e6 0%, #fef7e0 50%, #fff8e1 100%)'
    },
    blushing: {
        icon: '😳', name: '害羞',
        ttsTag: '[empathetic]',
        ttsTags: ['[empathetic]'],
        desc: 'Blushing — 脸颊泛红，害羞表情',
        expression: 'Blushing',
        motion: { group: 'Idle', index: undefined },
        bg: 'linear-gradient(160deg, #f3e8fe 0%, #fce8ee 50%, #fce4ec 100%)'
    },
    sad: {
        icon: '😢', name: '难过',
        ttsTag: '[sad]',
        ttsTags: ['[sad]', '[bored]', '[sighs]', '[crying]'],
        desc: 'Sad — 眼神黯淡，嘴角下垂',
        expression: 'Sad',
        motion: { group: 'Idle', index: undefined },
        bg: 'linear-gradient(160deg, #e3f2fd 0%, #e8eaf6 50%, #eceff1 100%)'
    }
};
```

### 4.2 模型加载与初始化

```javascript
const MODEL_URL = 'https://raw.githubusercontent.com/Live2D/CubismWebSamples/4-r.7/Samples/Resources/Natori/Natori.model3.json';

async function initLive2D(canvasElement, wrapperElement) {
    if (!PIXI || !PIXI.live2d) throw new Error('pixi-live2d-display 未加载');

    const w = wrapperElement.clientWidth;
    const h = wrapperElement.clientHeight;

    const app = new PIXI.Application({
        view: canvasElement,
        width: w,
        height: h,
        backgroundAlpha: 0,
        antialias: true,
        autoDensity: true,
        resolution: Math.min(window.devicePixelRatio || 1, 2),
    });

    const model = await PIXI.live2d.Live2DModel.from(MODEL_URL, {
        autoInteract: true,  // 鼠标视线跟随
    });

    // 缩放：上半身特写
    const scaleX = w / model.width * 0.9;
    const scaleY = h / model.height * 1.3;
    const scale  = Math.min(scaleX, scaleY);
    model.scale.set(scale);
    model.anchor.set(0.5, 0.35);
    model.x = w / 2;
    model.y = h * 0.7;

    app.stage.addChild(model);
    app.stage.interactive = true;
    app.stage.hitArea = new PIXI.Rectangle(0, 0, w, h);

    return { app, model };
}
```

### 4.3 情绪切换

```javascript
function setEmotion(model, wrapperElement, key) {
    const emo = EMOTIONS[key];
    if (!model || !emo) return;

    model.expression(emo.expression);
    model.motion(emo.motion.group, emo.motion.index);
    wrapperElement.style.background = emo.bg;
}
```

### 4.4 响应式缩放

```javascript
function setupResize(app, model, wrapperElement) {
    const onResize = () => {
        const nw = wrapperElement.clientWidth;
        const nh = wrapperElement.clientHeight;
        app.renderer.resize(nw, nh);
        const ns = Math.min(nw / model.width * 0.9, nh / model.height * 1.3);
        model.scale.set(ns);
        model.x = nw / 2;
        model.y = nh * 0.7;
        app.stage.hitArea = new PIXI.Rectangle(0, 0, nw, nh);
    };
    window.addEventListener('resize', onResize);
    return onResize;
}
```

### 4.5 点击交互

```javascript
function setupHitDetection(model) {
    model.on('hit', (hitAreas) => {
        if (hitAreas.includes('Head') || hitAreas.includes('Body')) {
            model.motion('TapBody');
        }
    });
}
```

---

## 5. 缩放参数说明

| 参数 | 当前值 | 含义 |
|---|---|---|
| `scaleX factor` | 0.9 | 模型宽度占 canvas 宽度 90% |
| `scaleY factor` | 1.3 | 模型高度超出 canvas 130%（裁掉下半身） |
| `anchor` | (0.5, 0.35) | 锚点水平居中，垂直偏上 35% |
| `model.y` | `h * 0.7` | 模型整体下移，只露上半身 |
| `resolution` | `min(devicePixelRatio, 2)` | 高清屏 2x，防止 4K 屏 GPU 爆炸 |

调整要点：
- 想看全身：`scaleY` 降到 `0.7`，`model.y` 改 `h * 0.5`，`anchor.y` 改 `0.4`
- 想只看脸：`scaleY` 升到 `1.8`，`model.y` 改 `h * 0.85`

---

## 6. LiveKit 集成规划

### 6.1 当前架构

```
浏览器                    Caddy (443)                Next.js Frontend (3000)
  │ HTTPS  ──────────────────►│ TLS 终止 ──────────────►│ /api/token 签 JWT
  │ WSS    ──────────────────►│                         ↓
  │                            │                       LiveKit Server (7880/7881)
  │ WebRTC (UDP 50000–60000) ─►│                         │ dispatch
  │                            │                         ▼
  │                            │                   Python Agent
  │                            │                     ├─ STT: Chirp 2
  │                            │                     ├─ LLM: Gemini 3.1 Pro
  │                            │                     └─ TTS: Gemini 3.1 Flash TTS
```

前端 fork: <https://github.com/yangwhale/agent-starter-react>
Agent fork: <https://github.com/yangwhale/voice-pipeline-agent-python>

### 6.2 集成方案：Data Channel

推荐方案：Agent 通过 LiveKit Data Channel 发送情绪元数据，前端订阅并渲染。

**Agent 侧** (Python)：
```python
from livekit import rtc

# Agent 在 TTS 生成时提取 emotion tag
emotion = extract_emotion_from_tts_text(tts_text)

# 通过 data channel 发送情绪事件
await room.local_participant.publish_data(
    json.dumps({"type": "emotion", "value": emotion}).encode(),
    kind=rtc.DataPacketKind.RELIABLE,
)
```

**Frontend 侧** (React)：
```tsx
import { useDataChannel } from '@livekit/components-react';

function Live2DAvatar() {
    const { model, wrapper } = useLive2D();

    useDataChannel('emotion', (msg) => {
        const data = JSON.parse(new TextDecoder().decode(msg.payload));
        if (data.type === 'emotion') {
            setEmotion(model, wrapper, data.value);
        }
    });

    return <canvas ref={canvasRef} />;
}
```

### 6.3 替代方案：TTS 文本内解析

从 TTS 音频转录或 Agent 发送的字幕中提取 `[tag]`。

```tsx
import { useTrackTranscription } from '@livekit/components-react';

function Live2DAvatar() {
    const transcription = useTrackTranscription(agentAudioTrack);

    useEffect(() => {
        if (transcription?.segments) {
            const latest = transcription.segments[transcription.segments.length - 1];
            const emotion = extractEmotion(latest.text);
            setEmotion(model, wrapper, emotion);
        }
    }, [transcription]);
}
```

### 6.4 方案对比

| 维度 | Data Channel | TTS 文本解析 |
|---|---|---|
| 延迟 | 低（独立通道） | 中（等 TTS 输出） |
| 可靠性 | 高（结构化 JSON） | 中（依赖 tag 格式） |
| 实现复杂度 | Agent + Frontend 都要改 | 只改 Frontend |
| 灵活性 | 可传任意元数据 | 只能传 tag 文本 |
| **推荐** | ✓ | 作为 fallback |

### 6.5 集成步骤 Checklist

1. [ ] `agent-starter-react` 中添加 Live2D React 组件
2. [ ] 安装 npm 依赖：`pixi.js@6.5.10`, `pixi-live2d-display@0.4.0`
3. [ ] 模型文件托管到自己的 CDN 或打包到 `public/`
4. [ ] Agent 侧添加 emotion 提取 + data channel 发送
5. [ ] Frontend 侧订阅 data channel + 调用 `setEmotion()`
6. [ ] 测试所有 8 种情绪的切换和回退
7. [ ] 移动端适配（canvas 高度、触摸事件）

---

## 7. 踩坑记录

### 7.1 TapBody motion 会导致画面跳动

**问题**: 调用 `model.motion('TapBody')` 时，某些 motion 文件会修改模型的 position/scale 参数，导致角色突然偏移。

**解决**: 自动播放只用 `Idle` group（不会改变 transform），`TapBody` 仅在用户点击时触发。

### 7.2 Cubism 5 模型报 Unknown error

**问题**: 从 CubismWebSamples `develop` 分支下载的模型加载时报 `Unknown error`。

**根因**: `develop` 分支的模型已升级到 Cubism 5 (moc3 version 6)，pixi-live2d-display 0.4.0 不支持。

**解决**: 使用 `4-r.7` tag 的模型。通过 `od` 命令检查 moc3 文件头确认版本。

### 7.3 表情切换用 expression() 不用 motion()

**问题**: 用 `model.motion()` 切换表情会打断当前的 idle 动画，造成不自然的停顿。

**解决**: 面部表情用 `model.expression(name)` 切换（叠加在 motion 之上），动作用 `model.motion(group, index)` 控制。两者独立不冲突。

### 7.4 canvas 缩放变形

**问题**: 窗口大小变化时模型被拉伸。

**解决**: 用 `Math.min(scaleX, scaleY)` 取较小值，保持模型比例。同时在 resize 事件中同步更新 renderer 和 hitArea。

### 7.5 PixiJS v7+ 不兼容

**问题**: pixi-live2d-display 0.4.0 是为 PixiJS v6 编写的，v7+ 的 API 有 breaking changes。

**解决**: 锁定 PixiJS 6.5.10。未来如需升级 PixiJS，需等 pixi-live2d-display 发布兼容版本。

---

## 8. 模型文件结构参考

一个完整的 Cubism 4 模型目录：

```
Natori/
├── Natori.model3.json          # 入口配置（引用下面所有文件）
├── Natori.moc3                 # 模型二进制（mesh + 骨骼 + 参数）
├── Natori.physics3.json        # 物理模拟（头发/衣物飘动）
├── Natori.pose3.json           # 姿态切换
├── Natori.cdi3.json            # 参数/部件显示名
├── expressions/
│   ├── Angry.exp3.json         # 表情文件：一组参数偏移值
│   ├── Blushing.exp3.json
│   ├── Sad.exp3.json
│   ├── Smile.exp3.json
│   ├── Surprised.exp3.json
│   ├── exp_02.exp3.json
│   ├── exp_03.exp3.json
│   └── ...
├── motions/
│   ├── Natori_Idle_01.motion3.json
│   ├── Natori_Idle_02.motion3.json
│   ├── Natori_Idle_03.motion3.json
│   ├── Natori_TapBody_01.motion3.json
│   └── ...
└── textures/
    └── Natori.2048/
        └── texture_00.png      # 2048×2048 角色贴图
```

自定义角色时，至少需要：`.model3.json` + `.moc3` + `textures/` + 若干 `.exp3.json`。

---

## 9. 授权说明

| 素材 | 授权 |
|---|---|
| Natori 模型 | [Live2D Free Material License](https://www.live2d.com/en/learn/sample/) — 可免费用于个人和商业项目 |
| pixi-live2d-display | MIT License |
| PixiJS | MIT License |
| Cubism Core Runtime | Live2D Proprietary — 免费使用但不可修改 |
