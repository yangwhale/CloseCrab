---
name: hk-bus
description: 香港智能通勤助手。Google Maps 路线推荐 + KMB/Citybus 实时到站融合。当用户说"几点有车"、"巴士到了吗"、"怎么去公司"、"坐什么车"、"查公交"、"bus ETA"、"下一班车"、"通勤"、"commute"等关键词时触发。
trigger: 巴士、公交、bus、ETA、几点有车、下一班车、坐几路、怎么去、坐什么车、通勤、commute、路线
---

# 香港智能通勤助手

Google Maps 路线推荐 + KMB/Citybus 实时到站，两步串联。

## 核心流程

1. 用户提供出发位置 → 调 Google Maps Directions API (transit) 获取推荐路线
2. 从路线中提取巴士线路 → 调 KMB/Citybus 实时 API 叠加精确到站时间
3. 比较步行到站时间 vs 巴士到站时间 → 判断赶不赶得上

## 用法

```bash
# 智能通勤（需要用户位置，自动融合 Google Maps + 实时 ETA）
python3 ~/CloseCrab/skills/hk-bus/scripts/bus-eta.py --origin "Cumberland Rd 3, Kowloon Tong"
python3 ~/CloseCrab/skills/hk-bus/scripts/bus-eta.py --origin "La Salle Rd 6, Kowloon City"
python3 ~/CloseCrab/skills/hk-bus/scripts/bus-eta.py --origin "22.3370,114.1750"

# 指定目的地（默认 Times Square, Causeway Bay）
python3 ~/CloseCrab/skills/hk-bus/scripts/bus-eta.py --origin "..." --dest "Central, Hong Kong"

# 传统模式（查单条路线的实时 ETA，不需要位置）
python3 ~/CloseCrab/skills/hk-bus/scripts/bus-eta.py 113
python3 ~/CloseCrab/skills/hk-bus/scripts/bus-eta.py --all
python3 ~/CloseCrab/skills/hk-bus/scripts/bus-eta.py --list
```

## 重要：调用前必须获取用户位置

用户问"坐什么车去公司"时，**必须先确认用户位置**再调脚本：
- 如果用户发了飞书定位消息 → 用经纬度作为 `--origin`
- 如果用户提到了地名 → 用地名作为 `--origin`
- 如果都没有 → 问用户"你现在在哪？"

**绝对不要**在不知道用户位置的情况下只查 `--all`，因为无法判断用户能否赶上某班车。

## 数据源

- Google Maps Directions API: 路线规划 + 步行时间 + 换乘方案
- KMB: `https://data.etabus.gov.hk/v1/transport/kmb/` (实时到站)
- Citybus: `https://rt.data.gov.hk/v2/transport/citybus/` (实时到站)
- 巴士数据免费、无需 API key、精确到秒
- Google Maps API key 从 `/tmp/maps-api-key.txt` 或 Firestore 读取
