# LiveKit 数字人（Avatar）接入

把语音 agent 的声音接到一个会说话的形象上。本文记录 **2026-09-16 在 8×B200 上把
阿里开源的 LiveAvatar 实际跑通** 的全过程：能跑的配置、踩过的坑、实测数字，
以及接进 CloseCrab 语音链路需要写的那部分代码。

> ⚠️ **同名陷阱，先说这个。** LiveKit 官方插件库里有一个叫 `LiveAvatar` 的插件，
> 那是 **HeyGen 的商业产品**（需要 `LIVEAVATAR_API_KEY`），跟本文说的阿里开源
> LiveAvatar **同名但毫无关系**。搜文档时极易搞混。

---

## 1. 为什么这条路走得通

LiveAvatar 吃的是**音频**，不是文字 —— README 原话是「从音频输入，加参考图，
加可选的文字提示，生成视频」。它们 TODO 里那条未完成的 `TTS integration`
对我们**不构成障碍**：那只是说官方 demo 没打包 TTS，你得自己喂声音。

而我们两条链路的出口本来就是音频：

| 链路 | 出口形态 | 改动 |
|---|---|---|
| Gemini Live 语音助手 | 模型直接输出音频 | 无 |
| CloseCrab bot（`voice/livekit_out.py`） | TTS → 48k PCM → `rtc.AudioSource.capture_frame` | 无 |

两条都已经是裸 PCM 流。**接数字人不需要改这两条链路的任何代码** ——
只是音频的去向从「直接推给用户」变成「先进数字人，再由数字人推出音视频」。

一个便于理解的说法：LiveAvatar 是个**配音演员**。给它一张照片和一段声音，
它把这个人演出来。它不识字，所以文字必须先变成声音 —— 而这一步我们早就有了。

---

## 2. LiveKit 侧怎么接

数字人在房间里是**另一个参与者**，不是 agent 进程的一部分。

```
用户 ──audio──▶ SFU ──▶ agent 进程（LLM / Gemini Live）
                              │ 生成的音频不直接发布
                              │ 走 DataStream
                              ▼
                        avatar worker（独立进程，持有 GPU）
                              │ 生成画面，音视频对齐
                              └──▶ SFU ──▶ 用户看到会说话的人
```

框架已经把调度、发布、音画同步做完了。我们要实现的只有 `VideoGenerator`
这一个协议，三个方法（`livekit/agents/voice/avatar/_types.py`）：

```python
class VideoGenerator(ABC):
    @abstractmethod
    async def push_audio(self, frame: rtc.AudioFrame | AudioSegmentEnd) -> None:
        """喂一帧音频进来"""

    @abstractmethod
    def clear_buffer(self) -> None | Coroutine[None, None, None]:
        """清空音频缓冲，立刻停止播放"""

    @abstractmethod
    def __aiter__(self) -> AsyncIterator[rtc.VideoFrame | rtc.AudioFrame | AudioSegmentEnd]:
        """持续吐出视频帧和音频帧"""
```

配套机制：
- avatar worker 的 token 要带 `lk.publish_on_behalf`（指向 agent 的 identity）
  和 `lk.avatar_provider`；token **必须服务端签发**
- `Room.agentParticipants` / `Participant.avatarWorker` 靠这个属性做正反向查找
- 客户端那边不用改 —— 数字人发布的就是普通的音视频轨

### ⚠️ `clear_buffer` 是这三个里最要命的

打断发生时画面必须**立刻**停。人已经不说话了而屏幕上的嘴还在动，
恐怖谷一下就掉进去了。CloseCrab 客户端的按住说话打断链路目前是通的
（见 `CCMicPolicy` 的 900ms release tail），接数字人时这一路要一路传到底。

---

## 3. 部署 runbook（实测可复现）

环境：8 卡 B200（sm_100 / Blackwell）、Ubuntu、1.9T 本地盘。
**实测整个环境 4 分 17 秒装完**，其中 46GB 基座模型下载只用了 45 秒。

```bash
sudo apt-get install -y ffmpeg git-lfs build-essential && git lfs install
git clone --depth 1 https://github.com/Alibaba-Quark/LiveAvatar.git && cd LiveAvatar

# uv 建 py3.10 venv，比 conda 快一个数量级
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv --python 3.10 .venv && source .venv/bin/activate
uv pip install torch==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cu128
uv pip install -r requirements.txt

# B200 是 Blackwell 不是 Hopper —— README 推荐的 FlashAttention 3 那条 wheel
# 是给 H800/H200 的，Blackwell 走 FA2。实测 2.8.3 装得上、能用。
uv pip install flash-attn==2.8.3 --no-build-isolation

# ⛔ 必须卸掉，理由见坑 #2
uv pip uninstall deepspeed

uv pip install "huggingface_hub[cli]"
hf download Wan-AI/Wan2.2-S2V-14B --local-dir ./ckpt/Wan2.2-S2V-14B   # 46GB
hf download Quark-Vision/Live-Avatar --local-dir ./ckpt/LiveAvatar     # 1.3GB
```

推理（在 `infinite_inference_multi_gpu.sh` 基础上改，**关键差异已标注**）：

```bash
export TORCHINDUCTOR_CACHE_DIR=/path/to/persistent/cache   # 跨次复用，省掉每次 ~34s 编译预热
CUDA_VISIBLE_DEVICES=0,1,2,3,4 .venv/bin/torchrun --nproc_per_node=5 \
  minimal_inference/s2v_streaming_interact.py \
  --task s2v-14B --size "720*400" \                        # ⛔ 面积有上限，超了崩，见坑 #3
  --training_config liveavatar/configs/s2v_causal_sft.yaml \
  --prompt "<描述画面内容和光照风格>" \
  --image  "<参考图>" --audio "<16kHz 单声道 wav>" \
  --infer_frames 48 --load_lora --lora_path_dmd "Quark-Vision/Live-Avatar" \
  --sample_steps 4 --sample_guide_scale 0 --num_clip 30 \
  --num_gpus_dit 4 --sample_solver euler --enable_vae_parallel \
  --ckpt_dir ckpt/Wan2.2-S2V-14B/ --fp8 \
  --save_file "/绝对路径/输出.mp4"                          # ⛔ 必须全路径带扩展名，见坑 #4
```

输出视频的**时长与音频完全一致**（实测 9 条，误差在一帧以内），
分辨率按参考图的长宽比走 —— 竖图出 384×704，横图出 704×384，25 fps。

---

## 4. 五个坑

### 坑 1 · 激活 venv 之后不要再改 PATH
`source .venv/bin/activate` 之后又 `export PATH="$HOME/.local/bin:$PATH"`，
系统的 `torchrun` 会盖掉 venv 里的，结果用系统 python 跑，venv 里几百个包
一个都看不见。**症状是一串 `ModuleNotFoundError`，看着像依赖没装全，
其实装得好好的。** 判据：看 traceback 里的 site-packages 路径是哪个 python 版本。

最稳的写法是直接用绝对路径 `.venv/bin/torchrun`，不依赖 PATH。

### 坑 2 · `deepspeed` 与 `transformers` 循环 import（官方 requirements 的坑）
照 README 装完，**直接跑不起来**：

```
transformers/modeling_utils.py:158        import deepspeed
  → deepspeed/runtime/hybrid_engine.py:26  transformers.models.opt.modeling_opt...
    → 回头要 PreTrainedModel，而 modeling_utils 还在初始化中
ImportError: cannot import name 'PreTrainedModel' from partially initialized module
```

`deepspeed` 在 requirements.txt 里是给训练用的，而**训练代码还没开源**，
推理完全不需要。卸掉即可，`transformers` 4.51.3 只在 deepspeed 存在时才 import 它。

> 这个坑的迷惑性在于报错说「circular import」，看着像 transformers 自己的 bug。
> 单独 `import transformers` 是好的，得 `import transformers.modeling_utils`
> 才能看到真正的第一因。

### 坑 3 · `--size` 有**面积上限**，超了就炸
参数表里列了 12 个档位，但配 LiveAvatar LoRA 的实时管线里
**KV cache 长度写死成 3000**，面积大到让 token 数超过它就崩：

```
RuntimeError: The expanded size of the tensor (3000) must match
              the existing size (3640) at non-singleton dimension 1
```

实测（每组只变一个量）：

| 参考图 | size | 实际输出 | 结果 |
|---|---|---|---|
| 官方样例图 | `720*400` | 704×384 | ✅ |
| 自制横图 | `720*400` | 704×384 | ✅ |
| 自制竖图 | `720*400` | 384×704 | ✅ |
| 自制横图 | `704*384` | 704×384 | ✅ |
| 官方样例图 | `832*480` | — | ❌ 3000 vs **3360** |
| 自制竖图 | `480*832` | — | ❌ 3000 vs **3640** |

三件事：

1. **变量是面积，不是图片也不是构图。** 用官方自己的图换大尺寸照样炸；
   用我自己的竖图跑小尺寸完全正常。
2. **`720*400` 和 `704*384` 对同一张图会 snap 到同一个实际分辨率**
   （都出 704×384），所以它们其实是一档，不是两档。耗时也一致
   （193.3 s vs 199.2 s，在噪声内）。
3. 3000 是**上限**不是定值 —— 小于它的面积没问题，超过就崩。
   剩下那些更大的档位没逐个测，但按这个规律应该都不行。

`size` 只是**面积预算**，最终长宽比跟着参考图走，所以竖构图用 `720*400`
一样出竖版视频（384×704），不用改这个参数。

### 坑 4 · `--save_file` 传短名会写丢
源码里是 `args.save_file = args.save_dir + args.save_file + suffix`
—— **字符串直接拼，中间没有路径分隔符**，而且这一段只在 `save_file is None`
时才执行。所以传 `--save_file smoke` 的后果是写到当前目录、文件名就叫
`smoke`、**没有 .mp4 扩展名**，`--save_dir` 被完全忽略。

传绝对路径 + 扩展名最省事。若要用 `--save_dir` 让它自动命名，
那个路径**必须带结尾斜杠**。

### 坑 5 · `pkill -f` 会杀掉自己
`pkill -f s2v_streaming_interact` 里那个字符串也在**你自己这条命令行**里，
ssh 会话当场被杀（退出码 255）。写成字符类断开自匹配：

```bash
pkill -f "s2v_streami[n]g"
```

---

## 5. 实测数据（8×B200，用 5 张）

配置：`720*400`、4 步采样、fp8、`ENABLE_COMPILE=true`、4 卡跑 DiT + 1 卡跑 VAE。

**显存**：DiT 那 4 张各 47.6 GB，VAE 那张 38.8 GB。B200 单卡 183 GB，
所以**显存远不是瓶颈** —— 卡数是被流水线切分方式决定的，不是被容量逼的。

**耗时**（9 条，视频 10.6–21.4 秒，对总耗时做线性回归）：

| 量 | 值 |
|---|---|
| 固定开销 | **≈175 s**（模型加载 + 编译 + 落盘） |
| 边际成本 | **≈1.39 s 计算 / 1 s 视频** → **0.72× 实时** |
| 拟合质量 | R² = 0.64，残差 −5.6 ~ +4.8 s，n=9 |

R² 只有 0.64，9 个点、残差 ±5 秒 —— 这个斜率**量级可信、精度有限**。

### ⚠️ 这条路径**没有达到实时**，跟官方宣称有差距

官方称多卡 H800 上 45 FPS，输出 25 fps，折合 1.8× 实时。我实测 0.72× 实时，
**差约 2.5 倍**。

一个容易犯的错误是看采样进度条下结论 —— 稳态时那个 4 步循环跑到 22 it/s，
折合 0.18 秒，看着快得离谱。但**那只是 DiT 去噪那一段**，不含 VAE 编解码、
wav2vec 特征提取、motion frame 拼接。端到端必须用「总时长 ÷ 视频时长」来量。

差距的可能原因（**均为推测，未验证**）：
1. 走的是离线批处理路径 —— 脚本名字虽然叫 `streaming_interact`，
   但一次性喂完整音频、最后落盘，阶段之间不一定像 TPP 流式那样重叠
2. Blackwell 上用的是 FA2，官方 H800 推荐 FA3
3. `num_frames_per_block` 等流水线参数没调

要判定能不能实时，得跑它真正的流式路径（`gradio_multi_gpu.sh`）再量一次。

---

## 6. 接进来还差什么

| 事项 | 状态 |
|---|---|
| 音频源 | ✅ 两条链路都已是 PCM，不用改 |
| `VideoGenerator` 三方法实现 | ⬜ 要写 |
| 打断链路接到 `clear_buffer` | ⬜ 要写 |
| 实时性达标 | ⬜ **未验证** —— 离线路径 0.72× 实时，流式路径没测 |
| 官方 TODO 未完成项 | 流式交互 UI、TTS 集成、训练代码 |

**选址结论**：avatar worker 该贴着 SFU（音频进去细、视频出来粗），
但现实约束是 GPU 在哪儿。8 卡 H100/B200 这类机型的可用区很有限，
先按「有卡的地方」定，跨区那点 RTT 在这条链里是零头 ——
端到端预算里光 VAD 等静音就 800 ms，生成侧又是几百毫秒起。

**部署形态**：数字人是交互式服务，不像训练能把卡跑满，
挂着不用很贵；且 Spot 实例被抢占会让对话中途消失。
要常驻得用按需实例，做 demo 用 Spot 没问题。
