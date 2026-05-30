---
name: weather-forecast
description: 查天气预报。香港用香港天文台官方 API（最准、秒回），全球用 Open-Meteo（免 key）。当用户说"天气"、"天气预报"、"今天天气"、"明天天气"、"几天天气"、"会不会下雨"、"weather"、"forecast"、"香港天气"、"某城市天气"等关键词时触发。
---

# 天气预报

多源快速天气查询。优先用免费、秒回、无需 key 的官方数据源。

## 触发条件

- "天气"、"天气预报"、"今天/明天天气"、"九天天气"、"会不会下雨"
- "香港天气"、"东京天气"、"<城市>天气"
- "weather"、"forecast"

## 用法

```bash
python3 ~/.claude/skills/weather-forecast/scripts/weather.py            # 默认香港
python3 ~/.claude/skills/weather-forecast/scripts/weather.py hk         # 香港天文台
python3 ~/.claude/skills/weather-forecast/scripts/weather.py "Tokyo"    # 任意城市
python3 ~/.claude/skills/weather-forecast/scripts/weather.py 22.3 114.17  # 经纬度
python3 ~/.claude/skills/weather-forecast/scripts/weather.py hk --json  # 原始 JSON
```

脚本已格式化好输出，直接转述给用户即可（聊天平台保持简洁，长预报可裁剪到 3-5 天）。

## 数据源说明

### 默认启用（免费、无需 key）

| 源 | 用途 | 端点 | 认证 |
|---|---|---|---|
| **香港天文台 HKO** | 香港地区（最准） | `data.weather.gov.hk/weatherAPI/opendata/weather.php` | 无 |
| **Open-Meteo** | 全球任意地点 | `api.open-meteo.com/v1/forecast` | 无 |

HKO dataType 参数：
- `flw` 本港地区天气预报（今明）
- `fnd` 九天天气预报
- `rhrread` 实时天气（气温/湿度/雨量/各区）
- `warnsum` 现行警告摘要
- `swt` 特别天气提示
- lang：`tc`(繁) / `sc`(简) / `en`(英)

天气图标：HKO 返回 `ForecastIcon` 编号，对应 `https://www.hko.gov.hk/images/HKOWxIconOutline/pic{编号}.png`

### 可选源（需配 key，默认未启用）

**Apple WeatherKit** — 苹果天气同款数据源（聚合多家气象局，含 HKO）。
- 端点：`weatherkit.apple.com/api/v1/weather/{lang}/{lat}/{lon}`
- 认证：Apple Developer 账号（$99/年）签 ES256 JWT
- 免费额度：50 万次/月
- 苹果天气 App 自 2022 起就用 WeatherKit（取代 2020 收购的 Dark Sky）

**Google Weather API**（Maps Platform，2025 GA）
- 端点：`weather.googleapis.com/v1/currentConditions:lookup`、`/forecast/days:lookup`、`/forecast/hours:lookup`
- 认证：GCP API key（`?key=`）
- 计费：按量，有免费额度

**墨迹天气** — 无免费公开 API，走 B2B 商业授权（企业版/开放平台），需付费商务合作。香港地区数据不如天文台准，不推荐个人使用。

要启用 WeatherKit/Google，在 `scripts/weather.py` 加对应 fetch 函数并把 key 放环境变量；本 skill 默认不依赖任何 key，开箱即用。

## 为什么之前网页访问慢

天文台官网 `hko.gov.hk` 是渲染页（含大量 JS/图表），抓取慢。换成上面的 **opendata JSON API** 直连即可秒回（实测各端点几百毫秒）。
