---
name: hk-bus
description: 香港实时巴士到站查询。KMB + Citybus 开放 API，精确到秒。当用户说"几点有车"、"巴士到了吗"、"113什么时候来"、"查公交"、"bus ETA"、"下一班车"等关键词时触发。
trigger: 巴士、公交、bus、ETA、几点有车、下一班车、坐几路
---

# 香港实时巴士到站查询

KMB + Citybus 实时 ETA，精确到秒，与香港出行易 APP 同源数据。

## 用法

```bash
# 查单条路线（从常用站点）
python3 ~/.claude/skills/hk-bus/scripts/bus-eta.py 113

# 查所有常用路线
python3 ~/.claude/skills/hk-bus/scripts/bus-eta.py --all

# 列出常用路线
python3 ~/.claude/skills/hk-bus/scripts/bus-eta.py --list
```

## 常用路线（九龙城附近上车）

| 路线 | 方向 | 上车站 |
|------|------|--------|
| 113 | 彩虹 → 堅尼地城 | 喇沙利道 |
| 182 | 愉翠苑 → 中環 | 九龍醫院 |
| 170 | 沙田站 → 華富 | 九龍醫院 |
| 103 | 竹園邨 → 蒲飛路 | 九龍醫院 |

## 数据源

- KMB: `https://data.etabus.gov.hk/v1/transport/kmb/`
- Citybus: `https://rt.data.gov.hk/v2/transport/citybus/`
- 免费、无需 API key、精确到秒
