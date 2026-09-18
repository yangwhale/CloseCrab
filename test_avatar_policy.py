#!/usr/bin/env python3
"""数字人开关的判定 —— 三个输入合成一个状态。

Chris 2026-09-17：「客户端那个是不是要 enable live avatar，是有一个状态开关，
还有它是不是在后台运行。在后台运行，压根就只能出声，所以这个也要把那个
property 传到服务器端。」

这份测试盯的是**两处换了也能跑、但语义是错的**的地方：

  · 判断顺序 —— visible 必须排在 service_ok 前面
  · 两个缺省值方向相反 —— want 缺省关，visible 缺省开

这类地方单看代码都「说得通」，所以必须有测试把选中的那一种钉住。
"""
import itertools
import sys

from closecrab.voice.avatar_policy import (
    ATTR_STATE,
    ATTR_VISIBLE,
    ATTR_WANT,
    AvatarState,
    decide,
    decide_for_room,
    decide_from_attributes,
    is_user_visible_problem,
    parse_flag,
    should_generate,
)

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  ✅", name)
    else:
        fail += 1
        print("  ❌", name, f"— {detail}" if detail else "")


S = AvatarState

print("\n── 八种组合的完整真值表 ──")
# 穷举，一个不落。列出来比写几条代表性用例强：以后有人改了分支顺序，
# 这张表会精确指出哪一格翻了。
TRUTH = {
    #  want,  visible, service → 期望
    (True, True, True): S.ON,
    (True, True, False): S.UNAVAILABLE,
    (True, False, True): S.HIDDEN,
    (True, False, False): S.HIDDEN,  # ⭐ 见下面单独一条
    (False, True, True): S.OFF,
    (False, True, False): S.OFF,
    (False, False, True): S.OFF,
    (False, False, False): S.OFF,
}
# ⚠️ 显式 respect_visibility=True：这条分支 2026-09-18 起**默认关闭**
#    （Chris：「前台判断先不要了，就看手动开关」），但分支本身留着，
#    所以真值表照测 —— 免得将来打开时才发现它悄悄坏了。
for want, visible, svc in itertools.product([True, False], repeat=3):
    got = decide(want=want, visible=visible, service_ok=svc, respect_visibility=True)
    exp = TRUTH[(want, visible, svc)]
    check(f"want={want:<5} visible={visible:<5} svc={svc:<5} → {exp.value}",
          got is exp, f"实得 {got.value}")

print("\n── 判断顺序：visible 必须排在 service_ok 前面 ──")
# ⭐ 这一条是整份测试的核心。两个分支交换顺序，上面的表只有这一格会变，
#    而它恰好是最容易被当成「无所谓」的那格。
#    后果：用户在后台时服务恰好挂了 → 报 unavailable → 他回到前台，
#    界面上挂着一条「数字人暂不可用」的假警报，而服务其实早恢复了。
r = decide(want=True, visible=False, service_ok=False, respect_visibility=True)
check("⭐ 后台 + 服务挂了 → hidden（不是 unavailable）",
      r is S.HIDDEN, f"实得 {r.value} —— 分支顺序被换了？")

# ⭐ want 必须能一票否决。用户自己关掉的功能，不该因为服务器状态
#    而向他报任何错 —— 那是在为他没要的东西道歉。
for visible, svc in itertools.product([True, False], repeat=2):
    r = decide(want=False, visible=visible, service_ok=svc, respect_visibility=True)
    check(f"⭐ want=False 一票否决（visible={visible} svc={svc}）", r is S.OFF, r.value)

print("\n── 哪些状态要占资源 / 要报错 ──")
check("只有 ON 去占 worker", [s for s in S if should_generate(s)] == [S.ON],
      str([s.value for s in S if should_generate(s)]))
# ⭐ OFF 和 HIDDEN 都不该在界面上报错：
#    OFF 是用户自己关的，HIDDEN 他压根看不见屏幕。报了就是纯噪音。
check("⭐ 只有 UNAVAILABLE 需要告诉用户",
      [s for s in S if is_user_visible_problem(s)] == [S.UNAVAILABLE],
      str([s.value for s in S if is_user_visible_problem(s)]))

print("\n── parse_flag：认不出来退默认，绝不抛 ──")
for raw in ("1", "true", "TRUE", "  True  ", "yes", "on"):
    check(f"{raw!r} → True", parse_flag(raw, default=False) is True)
for raw in ("0", "false", "FALSE", " off ", "no"):
    check(f"{raw!r} → False", parse_flag(raw, default=True) is False)
# ⭐ 抛异常会让整次属性变更事件丢掉，而客户端不会重发 —— 状态永久卡死。
#    退回默认值至少是个确定的行为。
for raw in ("", "   ", "maybe", "２", None):
    check(f"⭐ {raw!r} 认不出 → 用 default（两个方向都试）",
          parse_flag(raw, default=True) is True
          and parse_flag(raw, default=False) is False)

print("\n── 两个缺省值方向相反，各有各的理由 ──")
# ⭐ 老客户端一个属性都不发。默认开的话，每个连进来的旧客户端都去抢一路
#    GPU 槽位（一共才 8 路），而它连显示数字人的界面都没有。
check("⭐ 什么都没发的老客户端 → OFF（不抢槽位）",
      decide_from_attributes({}, service_ok=True) is S.OFF)

# ⭐ 反过来：明确要了、但没报可见性（实现了一半的版本），默认成不可见
#    会让它**永远停在 hidden**，而且没有任何报错 —— 纯粹「功能不工作」，
#    最难查的那种。默认放行最坏是白渲染一会儿。
check("⭐ 只发了 want、没发 visible → ON（不卡死在 hidden）",
      decide_from_attributes({ATTR_WANT: "true"}, service_ok=True) is S.ON)

# 2026-09-18 起默认不看 visible，所以这里是 ON 而不是 HIDDEN。
# 属性照收照存，只是不再据此决策。
check("want=true + visible=false → ON（默认不看前台）",
      decide_from_attributes(
          {ATTR_WANT: "true", ATTR_VISIBLE: "false"}, service_ok=True) is S.ON)
check("发了 want=false，visible 是什么都无所谓",
      decide_from_attributes(
          {ATTR_WANT: "false", ATTR_VISIBLE: "true"}, service_ok=True) is S.OFF)
# 属性字典里混着别人的键（LiveKit 自己的 lk.* 全在这里面），不能被干扰
check("字典里混着 lk.* 等无关键不受影响",
      decide_from_attributes(
          {"lk.agent.state": "speaking", ATTR_WANT: "1"}, service_ok=True) is S.ON)

print("\n── 一屋子客户端合成一个状态 ──")
WANT_VIS = {ATTR_WANT: "true", ATTR_VISIBLE: "true"}
WANT_BG = {ATTR_WANT: "true", ATTR_VISIBLE: "false"}
NOPE = {ATTR_WANT: "false"}

check("空房间 → OFF", decide_for_room({}, service_ok=True) is S.OFF)
check("一个人要且看得见 → ON",
      decide_for_room({"a": WANT_VIS}, service_ok=True) is S.ON)
# ⭐ 偏向开：关掉开关的那个人不能把别人的画面一起掐了。
#    一条视频轨全房共享，多一个人看不多花一分钱。
check("⭐ 一人开一人关 → ON（关的那个不能掐别人）",
      decide_for_room({"a": WANT_VIS, "b": NOPE}, service_ok=True) is S.ON)
check("⭐ 顺序反过来结果一样（聚合不能依赖字典顺序）",
      decide_for_room({"b": NOPE, "a": WANT_VIS}, service_ok=True) is S.ON)
# 原来这条是「都在后台 → HIDDEN（不占槽位）」。2026-09-18 默认关掉前台判断
# 之后变成 ON —— **代价就在这儿**：锁屏/后台时照样挂一路 GPU。
# 这是明知的取舍，不是回归；要还原设 CC_AVATAR_RESPECT_VISIBILITY=1。
check("都在后台 → ON（前台判断已停用，代价是照样占槽位）",
      decide_for_room({"a": WANT_BG, "b": WANT_BG}, service_ok=True) is S.ON)
# ⭐ 一个在前台一个在后台：前台那位要看，必须开。
check("⭐ 一前台一后台 → ON",
      decide_for_room({"a": WANT_BG, "b": WANT_VIS}, service_ok=True) is S.ON)
check("都没要 → OFF", decide_for_room({"a": NOPE, "b": NOPE}, service_ok=True) is S.OFF)
check("有人要、看得见、但服务挂了 → UNAVAILABLE",
      decide_for_room({"a": WANT_VIS}, service_ok=False) is S.UNAVAILABLE)
# ⭐ UNAVAILABLE 要盖过 HIDDEN：屋里有人正看着而服务挂了，得让他知道；
#    只因为另一个人在后台就降级成 hidden，那条提示就永远不出现。
check("⭐ 一个前台一个后台 + 服务挂了 → UNAVAILABLE（不被 HIDDEN 盖住）",
      decide_for_room({"a": WANT_BG, "b": WANT_VIS}, service_ok=False) is S.UNAVAILABLE)
# 没要的人不该把房间拖进 UNAVAILABLE —— 服务挂没挂跟他无关。
check("只有没要的人在，服务挂了也是 OFF",
      decide_for_room({"a": NOPE}, service_ok=False) is S.OFF)

print("\n── 属性键名与线上格式 ──")
# 键名一改，客户端和服务端就对不上了，而且**两边都不会报错** —— 服务端
# 读不到就按缺省当「没要」，现象是「开关拨了没反应」。所以钉死。
check("键名就是这三个", (ATTR_WANT, ATTR_VISIBLE, ATTR_STATE)
      == ("cc.avatar.want", "cc.client.visible", "cc.avatar.state"),
      f"{ATTR_WANT} {ATTR_VISIBLE} {ATTR_STATE}")
check("三个键都带 cc. 前缀（别跟 LiveKit 的 lk.* 撞）",
      all(k.startswith("cc.") for k in (ATTR_WANT, ATTR_VISIBLE, ATTR_STATE)))
# ⭐ AvatarState 继承 str，所以能直接塞进 attributes（那里只收字符串）。
#    改成普通 Enum 的话，写出去会变成 "AvatarState.ON" 这种字符串，
#    客户端解不出来 —— 而服务端这边毫无异常。
check("⭐ 状态能直接当字符串写进 attributes",
      isinstance(S.ON, str) and f"{S.ON.value}" == "on" and str(S.ON.value) == "on")

print("\n── ⭐ 默认不看前台（2026-09-18 起）──")
# Chris：「app 那个前台判断也先不要了，简化逻辑先，就看数字人手动开关」。
# 关掉的理由不只是省事 —— 它跟实际用法冲突：数字人由 bot 的播报驱动，
# 而让 bot 播报得先去飞书说话，一说话 app 就进后台，于是「想看」和
# 「能让它说话」在操作上互斥，判定永远停在 hidden。
for svc, exp in ((True, S.ON), (False, S.UNAVAILABLE)):
    r = decide(want=True, visible=False, service_ok=svc)
    check(f"⭐ 后台 + 服务{'好' if svc else '挂'} → {exp.value}（默认不看 visible）",
          r is exp, f"实得 {r.value}")
check("要恢复只要传 respect_visibility=True",
      decide(want=True, visible=False, service_ok=True,
             respect_visibility=True) is S.HIDDEN)
check("want=False 仍然一票否决",
      decide(want=False, visible=True, service_ok=True) is S.OFF)

print(f"\n{'=' * 52}\n通过 {ok} 条，失败 {fail} 条")
sys.exit(1 if fail else 0)
