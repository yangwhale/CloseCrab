"""要不要给这一路开数字人 —— 纯判定，不碰网络不碰 LiveKit。

单独一个文件、只依赖标准库，是为了**能离线把它钉死**。这个判断有三个输入、
四种结果，而且错了都是「静默」的那种：该开没开，用户以为坏了；不该开却开了，
白占一路 GPU 而屏幕上根本看不见。

## 三个输入，两边各管各的

    客户端说：  want     用户那个开关拨没拨
                visible  app 现在看得见吗（前台 / 后台 / 锁屏）
    服务端说：  service  数字人服务可不可用（网关通不通、还有没有空槽）

**客户端不做最终决定，服务端也不猜客户端的意图** —— 各报各的事实，
在这里合成。

## 为什么 visible 这一条值钱

不是省一点，是**省掉一整张 GPU 卡**：网关一共 8 路槽位，给一个没人看的画面
占着一路是纯浪费。而且 app 在后台时本来就只出声（iOS 上锁屏没法显示实时视频，
PiP 锁屏即停、CallKit 显示不了远端画面、Live Activity 是快照）。

## ⚠️ 回滞不在这里

「后台多久才算真的看不见」是**客户端**的事（见 iOS 侧 CCAvatarPresence）。
这里收到的 `visible` 已经是去抖之后的结论。
放在这一层会让服务端凭空多一个计时器，而它根本不知道用户刚才是不是
只拉了一下通知中心。
"""

from __future__ import annotations

import os
from enum import Enum


class AvatarState(str, Enum):
    """最终状态。**同时也是回报给客户端的那个值** —— 客户端据此决定
    是显示「正在接入」还是「数字人暂不可用」，而不是干等一条永远不来的视频轨。
    """

    ON = "on"
    """开。"""

    OFF = "off"
    """用户自己关的。客户端不该显示任何「出错了」的提示。"""

    HIDDEN = "hidden"
    """用户要，但他现在看不见（后台/锁屏），所以先不生成。
    回前台会自己变回 on —— **这不是错误状态**，客户端别报错。"""

    UNAVAILABLE = "unavailable"
    """用户要、也看得见，但服务端给不了（网关挂了 / 槽位满了）。
    **这一条必须让用户看见** —— 否则他只会觉得「怎么没有脸」。"""


RESPECT_VISIBILITY = os.environ.get("CC_AVATAR_RESPECT_VISIBILITY", "0") == "1"
"""要不要把「app 在不在前台」算进判定。**2026-09-18 起默认关掉。**

Chris 的原话：「app 那个前台判断也先不要了，简化逻辑先，就看数字人手动开关」。

关掉的理由不只是省事 —— 它跟实际用法冲突：数字人由 bot 的播报驱动，而让
bot 播报得先去飞书里说话，一说话 app 就进后台了。于是「想看数字人」和
「能让它说话」这两件事**在操作上互斥**，判定结果永远停在 `hidden`。

代价说清楚：锁屏和后台时照样会挂一路 GPU 渲染，而屏幕上根本看不见 ——
那正是当初加这一条要省的东西。所以它是暂时关掉，不是删掉：
设 `CC_AVATAR_RESPECT_VISIBILITY=1` 就回来。

客户端**仍然照常上报** `cc.client.visible`，只是这一层不再据此决策 ——
数据留着，将来要恢复不用改客户端。
"""


def decide(*, want: bool, visible: bool, service_ok: bool,
           respect_visibility: bool | None = None) -> AvatarState:
    """把输入合成一个状态。

    判断顺序是有讲究的，**不能换**：

    1. `want` 最先 —— 用户关掉了就到此为止。后面两条再怎么样都不该
       让客户端看到 `unavailable` 之类的提示：他没要，服务好不好跟他无关。
    2. `visible` 其次（**默认已停用**，见 `RESPECT_VISIBILITY`）—— 启用时
       它**必须排在 service 前面**：服务正好挂了、而用户又在后台时，
       该报的是 `hidden` 不是 `unavailable`；报后者会让他回前台后看到
       一条「暂不可用」的假警报。
    3. `service_ok` 最后 —— 到这儿才是真的「想要、能看见、但给不了」。
    """
    if respect_visibility is None:
        respect_visibility = RESPECT_VISIBILITY
    if not want:
        return AvatarState.OFF
    if respect_visibility and not visible:
        return AvatarState.HIDDEN
    if not service_ok:
        return AvatarState.UNAVAILABLE
    return AvatarState.ON


# LiveKit 的 participant attributes 只能装字符串，所以布尔要自己解。
ATTR_WANT = "cc.avatar.want"
"""客户端写：用户那个开关。"""

ATTR_VISIBLE = "cc.client.visible"
"""客户端写：现在看得见吗（已经过客户端侧去抖）。"""

ATTR_STATE = "cc.avatar.state"
"""**服务端写**，回报最终状态。客户端订阅它来决定界面怎么显示。"""

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def parse_flag(raw: str | None, *, default: bool) -> bool:
    """把属性里的字符串解成布尔。**认不出来就用 default，不抛异常。**

    不抛是故意的：这条路径在房间事件回调里，抛出去只会让一次属性变更
    整个丢掉，而客户端不会重发 —— 状态就永久卡住了。认不出来退回默认值
    至少还是个确定的行为。
    """
    if raw is None:
        return default
    s = raw.strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return default


def decide_from_attributes(attrs: dict[str, str], *, service_ok: bool) -> AvatarState:
    """从客户端属性直接算状态。两个默认值**方向相反**，都是故意的：

    - `want` 缺省 **False** —— 老客户端根本不发这个属性。默认开的话，
      每个连进来的旧客户端都会去抢一路 GPU 槽位，而它连显示的界面都没有。
    - `visible` 缺省 **True** —— 一个已经明确要 avatar、却没报可见性的客户端
      （比如只实现了一半的版本），默认成不可见会让它**永远停在 hidden**，
      而且没有任何报错，纯粹「功能不工作」。默认可见最坏是白渲染一会儿。

    一句话：**没表态的默认关，表了态但信息不全的默认放行。**
    """
    return decide(
        want=parse_flag(attrs.get(ATTR_WANT), default=False),
        visible=parse_flag(attrs.get(ATTR_VISIBLE), default=True),
        service_ok=service_ok,
    )


def decide_for_room(
    per_participant: dict[str, dict[str, str]], *, service_ok: bool
) -> AvatarState:
    """一屋子客户端合成**一个**状态。

    ## 为什么不能一人一个状态

    数字人是**一条视频轨**，全房共享 —— LiveKit 里没有「只发给某个人」这回事。
    所以「开不开」只能有一个答案。同理，回报用的 `cc.avatar.state` 写在
    我们自己这个 participant 上，也只有一份。

    ## 聚合规则：任何一个人要、且看得见，就开

    这是**故意偏向开**的：Chris 在 iPhone 上看着，屋里另一个人把开关关了 ——
    这时候必须继续发，否则关掉开关的那个人把别人的画面也掐了。
    一条轨的成本是固定的，多一个人看不多花钱。

    ## 回报的是「网关这一路的状态」，不是「你的状态」

    所以客户端**不能**把它当成唯一判据 —— 自己开关是关的却看到 `on`，
    那是别人开着，正常。客户端的规则是：自己开关开着 **且** 收到
    `unavailable` 才提示用户。两侧都要按这个约定写，见 iOS 的 `CCAvatarLink`。
    """
    if not per_participant:
        return AvatarState.OFF
    states = [
        decide_from_attributes(attrs, service_ok=service_ok)
        for attrs in per_participant.values()
    ]
    # 按「谁的诉求最强」排：有人真的在看 > 有人要但服务给不了 >
    # 有人要但看不见 > 没人要。**顺序跟 decide() 里的不是一回事**，
    # 那边是三个条件的短路，这边是多个已定状态之间的优先级。
    for s in (AvatarState.ON, AvatarState.UNAVAILABLE, AvatarState.HIDDEN):
        if s in states:
            return s
    return AvatarState.OFF


def should_generate(state: AvatarState) -> bool:
    """这个状态下要不要真的去占一路 worker。

    单独抽出来而不是让调用方写 `state == ON`：以后要加状态（比如
    `DEGRADED` 只发低帧率）时，占不占资源这件事有一个地方改。
    """
    return state is AvatarState.ON


def is_user_visible_problem(state: AvatarState) -> bool:
    """这个状态该不该在客户端界面上报出来。

    只有 `UNAVAILABLE` 该报 —— `OFF` 是用户自己关的，`HIDDEN` 是他看不见
    （也就无所谓看不看得到提示），两个都报出来就是噪音。
    """
    return state is AvatarState.UNAVAILABLE
