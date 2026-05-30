#!/usr/bin/env python3
"""天气预报 — 多源快速天气查询。

数据源优先级:
  - 香港 (HK) → 香港天文台 HKO 官方 API (最准、秒回、免 key)
  - 其他地区 → Open-Meteo (全球、免 key、含 WMO 天气码)

可选源 (需配 key, 默认不启用): Apple WeatherKit / Google Weather API
见 SKILL.md。

用法:
  weather.py                 # 默认香港 (天文台九天预报 + 实时)
  weather.py hk              # 香港天文台
  weather.py "Tokyo"         # 任意城市 (Open-Meteo + 地理编码)
  weather.py 22.3 114.17     # 经纬度
  weather.py hk --json       # 原始 JSON
"""
import sys
import json
import urllib.request
import urllib.parse

TIMEOUT = 8

# WMO weather_code → (emoji, 中文)
WMO = {
    0: ("☀️", "晴"), 1: ("🌤️", "大致晴朗"), 2: ("⛅", "局部多云"), 3: ("☁️", "阴"),
    45: ("🌫️", "雾"), 48: ("🌫️", "霜雾"),
    51: ("🌦️", "毛毛雨(小)"), 53: ("🌦️", "毛毛雨(中)"), 55: ("🌦️", "毛毛雨(大)"),
    61: ("🌧️", "小雨"), 63: ("🌧️", "中雨"), 65: ("🌧️", "大雨"),
    66: ("🌧️", "冻雨(小)"), 67: ("🌧️", "冻雨(大)"),
    71: ("🌨️", "小雪"), 73: ("🌨️", "中雪"), 75: ("❄️", "大雪"), 77: ("❄️", "雪粒"),
    80: ("🌦️", "阵雨(小)"), 81: ("🌦️", "阵雨(中)"), 82: ("⛈️", "阵雨(大)"),
    85: ("🌨️", "阵雪(小)"), 86: ("🌨️", "阵雪(大)"),
    95: ("⛈️", "雷暴"), 96: ("⛈️", "雷暴伴冰雹(小)"), 99: ("⛈️", "雷暴伴冰雹(大)"),
}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "weather-skill/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------- 香港天文台 ----------
def hko_url(dtype, lang="sc"):
    return f"https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType={dtype}&lang={lang}"


def hko(as_json=False, lang="sc"):
    flw = fetch(hko_url("flw", lang))
    fnd = fetch(hko_url("fnd", lang))
    rhr = fetch(hko_url("rhrread", lang))
    warn = fetch(hko_url("warnsum", lang))

    if as_json:
        print(json.dumps({"flw": flw, "fnd": fnd, "rhrread": rhr, "warnsum": warn},
                         ensure_ascii=False, indent=2))
        return

    out = ["🇭🇰 香港天气预报 (香港天文台)\n"]

    # 实时气温 (取香港天文台总部 / 京士柏)
    temps = rhr.get("temperature", {}).get("data", [])
    hum = rhr.get("humidity", {}).get("data", [])
    hko_t = next((t for t in temps if "天文台" in t.get("place", "")), temps[0] if temps else None)
    if hko_t:
        line = f"🌡️ 实时: {hko_t['value']}°C ({hko_t['place']})"
        if hum:
            line += f"  湿度 {hum[0]['value']}%"
        out.append(line)

    # 警告
    if warn:
        names = [v.get("name", "") for v in warn.values() if isinstance(v, dict) and v.get("name")]
        if names:
            out.append("⚠️ 现行警告: " + "、".join(names))

    # 今明预报
    out.append(f"\n📍 {flw.get('forecastPeriod', '本港预报')}")
    out.append(flw.get("forecastDesc", "").strip())
    if flw.get("tcInfo"):
        out.append("🌀 " + flw["tcInfo"].strip())

    # 九天
    out.append("\n📅 九天预报:")
    for d in fnd.get("weatherForecast", []):
        date = d["forecastDate"]
        date = f"{date[4:6]}/{date[6:8]}"
        wk = d.get("week", "")
        lo = d.get("forecastMintemp", {}).get("value", "?")
        hi = d.get("forecastMaxtemp", {}).get("value", "?")
        wx = d.get("forecastWeather", "").strip()
        out.append(f"  {date} {wk}: {lo}-{hi}°C  {wx}")

    print("\n".join(out))


# ---------- Open-Meteo (全球) ----------
def geocode(name):
    url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
        {"name": name, "count": 1, "language": "zh"})
    r = fetch(url)
    res = r.get("results")
    if not res:
        return None
    g = res[0]
    return g["latitude"], g["longitude"], g.get("name", name), g.get("country", "")


def open_meteo(lat, lon, label="", as_json=False):
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,apparent_temperature",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": "auto", "forecast_days": 7,
    })
    d = fetch(url)
    if as_json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return

    cur = d.get("current", {})
    code = cur.get("weather_code", 0)
    emoji, desc = WMO.get(code, ("🌡️", f"code {code}"))
    out = [f"📍 {label or f'{lat},{lon}'} 天气预报 (Open-Meteo)\n"]
    out.append(f"{emoji} 实时: {cur.get('temperature_2m', '?')}°C ({desc})  "
               f"体感 {cur.get('apparent_temperature', '?')}°C")
    out.append(f"   湿度 {cur.get('relative_humidity_2m', '?')}%  "
               f"风速 {cur.get('wind_speed_10m', '?')} km/h")

    daily = d.get("daily", {})
    dates = daily.get("time", [])
    out.append("\n📅 七天预报:")
    for i, dt in enumerate(dates):
        c = daily["weather_code"][i]
        e, ds = WMO.get(c, ("🌡️", f"code {c}"))
        lo = daily["temperature_2m_min"][i]
        hi = daily["temperature_2m_max"][i]
        pop = daily.get("precipitation_probability_max", [None] * len(dates))[i]
        popstr = f"  ☔{pop}%" if pop is not None else ""
        out.append(f"  {dt[5:]} {e} {ds}  {lo}-{hi}°C{popstr}")
    print("\n".join(out))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    as_json = "--json" in sys.argv

    # 默认 / 显式香港
    if not args or args[0].lower() in ("hk", "hongkong", "香港", "hong kong"):
        hko(as_json=as_json)
        return

    # 经纬度
    if len(args) == 2:
        try:
            lat, lon = float(args[0]), float(args[1])
            open_meteo(lat, lon, as_json=as_json)
            return
        except ValueError:
            pass

    # 城市名
    name = " ".join(args)
    g = geocode(name)
    if not g:
        print(f"❌ 找不到地点: {name}", file=sys.stderr)
        sys.exit(1)
    lat, lon, gname, country = g
    open_meteo(lat, lon, label=f"{gname} {country}".strip(), as_json=as_json)


if __name__ == "__main__":
    main()
