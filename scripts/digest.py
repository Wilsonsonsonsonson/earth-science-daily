#!/usr/bin/env python3
"""Fetch new earth-science items, ask Gemini to pick the noteworthy ones, post to Discord."""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

LOOKBACK_HOURS = 30          # cron drift buffer on top of the 24h cadence
MAX_ITEMS_PER_SOURCE = 12    # cap noisy feeds before they reach the LLM
SEEN_RETENTION_DAYS = 14     # how long a link is remembered to avoid repeats
DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "seen.json"

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
)

# category: "seismo" = 地球物理與地震學 (主要焦點), "other" = 其他地球科學領域
RSS_SOURCES = [
    {"name": "arXiv physics.geo-ph (地球物理預印本)", "url": "https://rss.arxiv.org/rss/physics.geo-ph", "category": "seismo"},
    {"name": "JGR: Solid Earth (AGU)", "url": "https://agupubs.onlinelibrary.wiley.com/action/showFeed?type=etoc&feed=rss&jc=21699356", "category": "seismo"},
    {"name": "Geophysical Research Letters (AGU)", "url": "https://agupubs.onlinelibrary.wiley.com/action/showFeed?type=etoc&feed=rss&jc=19448007", "category": "seismo"},
    {"name": "Solid Earth (EGU/Copernicus)", "url": "https://se.copernicus.org/xml/rss2_0.xml", "category": "seismo"},
    {"name": "Smithsonian GVP 全球火山活動週報", "url": "https://volcano.si.edu/news/WeeklyVolcanoRSS.xml", "category": "seismo"},
    {"name": "Nature Geoscience", "url": "http://feeds.nature.com/ngeo/rss/current", "category": "other"},
    {"name": "NHESS 自然災害與地球系統科學 (EGU/Copernicus)", "url": "https://nhess.copernicus.org/xml/rss2_0.xml", "category": "other"},
    {"name": "Eos (AGU 新聞)", "url": "https://eos.org/feed", "category": "other"},
]

USGS_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"
SEISMICPORTAL_URL = "https://www.seismicportal.eu/fdsnws/event/1/query"


def load_seen() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return {}


def save_seen(seen: dict) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SEEN_RETENTION_DAYS)).isoformat()
    pruned = {link: ts for link, ts in seen.items() if ts > cutoff}
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(pruned, ensure_ascii=False, indent=2))


def within_lookback(published: datetime, now: datetime) -> bool:
    return now - published <= timedelta(hours=LOOKBACK_HOURS)


def fetch_rss(source: dict, now: datetime) -> list:
    items = []
    try:
        parsed = feedparser.parse(source["url"])
    except Exception as exc:
        print(f"[warn] failed to fetch {source['name']}: {exc}", file=sys.stderr)
        return items

    for entry in parsed.entries[:MAX_ITEMS_PER_SOURCE]:
        struct = entry.get("published_parsed") or entry.get("updated_parsed")
        if struct:
            published = datetime.fromtimestamp(time.mktime(struct), tz=timezone.utc)
            if not within_lookback(published, now):
                continue
        else:
            published = now  # feed without dates: assume fresh, let de-dup catch repeats
        items.append(
            {
                "source": source["name"],
                "category": source["category"],
                "title": entry.get("title", "(無標題)").strip(),
                "link": entry.get("link", ""),
                "summary": (entry.get("summary", "") or "")[:400],
                "published": published.isoformat(),
            }
        )
    return items


def fetch_usgs(now: datetime) -> list:
    items = []
    try:
        resp = requests.get(USGS_URL, timeout=20)
        resp.raise_for_status()
        features = resp.json().get("features", [])
    except Exception as exc:
        print(f"[warn] failed to fetch USGS feed: {exc}", file=sys.stderr)
        return items

    for feature in features[:MAX_ITEMS_PER_SOURCE * 2]:
        props = feature.get("properties", {})
        ts_ms = props.get("time")
        published = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms else now
        if not within_lookback(published, now):
            continue
        items.append(
            {
                "source": "USGS 地震速報 (M4.5+)",
                "category": "seismo",
                "title": props.get("title", "(無標題)"),
                "link": props.get("url", ""),
                "summary": f"規模 {props.get('mag')}，深度資訊見連結，地點：{props.get('place')}",
                "published": published.isoformat(),
            }
        )
    return items


def fetch_seismicportal(now: datetime) -> list:
    items = []
    start = (now - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")
    params = {
        "format": "json",
        "start": start,
        "minmag": 5.0,
        "orderby": "time",
        "limit": MAX_ITEMS_PER_SOURCE * 2,
    }
    try:
        resp = requests.get(SEISMICPORTAL_URL, params=params, timeout=20)
        resp.raise_for_status()
        features = resp.json().get("features", [])
    except Exception as exc:
        print(f"[warn] failed to fetch EMSC/seismicportal feed: {exc}", file=sys.stderr)
        return items

    for feature in features:
        props = feature.get("properties", {})
        event_id = props.get("unid") or feature.get("id", "")
        link = f"https://www.emsc-csem.org/Earthquake/earthquake.php?id={event_id}"
        items.append(
            {
                "source": "EMSC 歐洲地中海地震中心 (M5.0+)",
                "category": "seismo",
                "title": f"M{props.get('mag')} - {props.get('flynn_region', '未知地區')}",
                "link": link,
                "summary": f"規模 {props.get('mag')} {props.get('magtype', '')}，深度 {props.get('depth')} 公里",
                "published": props.get("time", now.isoformat()),
            }
        )
    return items


def gather_candidates() -> list:
    """Return new-since-last-run items. Does NOT persist yet — call mark_seen()
    only after the digest has been posted successfully, so a failed run doesn't
    silently drop items."""
    now = datetime.now(timezone.utc)
    seen = load_seen()
    candidates = []

    for source in RSS_SOURCES:
        candidates.extend(fetch_rss(source, now))
    candidates.extend(fetch_usgs(now))
    candidates.extend(fetch_seismicportal(now))

    return [c for c in candidates if c["link"] and c["link"] not in seen]


def mark_seen(candidates: list) -> None:
    now = datetime.now(timezone.utc)
    seen = load_seen()
    for c in candidates:
        seen[c["link"]] = now.isoformat()
    save_seen(seen)


def build_prompt(candidates: list) -> str:
    lines = []
    for c in candidates:
        lines.append(
            f"- [{c['category']}] 來源：{c['source']} | 標題：{c['title']} | "
            f"時間：{c['published']} | 連結：{c['link']} | 摘要：{c['summary']}"
        )
    candidate_block = "\n".join(lines) if lines else "(今日無新候選項目)"

    return f"""你是一份地球科學日報的編輯，讀者是對地球科學有興趣的一般讀者，會在吃早餐時瀏覽。

你的任務：從下面的候選項目清單中，篩選出「真正值得一看」的內容，其餘全部丟棄。候選項目分兩類標籤：
- [seismo]：地球物理與地震學，這是本日報的主要焦點，請優先且較寬鬆地納入（例如規模較大或有感地震、重要新論文、火山活動明顯變化）。
- [other]：其他地球科學領域（地質、大氣、海洋、行星科學等），作為次要補充，只挑真正有意思或重要的 1-3 則即可，不必每天都有。

篩選原則：
- 忽略例行、重複、規模很小或無明顯新聞價值的項目。
- 同一事件如果被多個來源報導，合併成一則，並可附上主要連結。
- 如果某類別今天真的沒有值得報導的內容，誠實寫「今日無重大更新」，不要硬湊字數。

請用繁體中文輸出一則適合傳到 Discord 的日報訊息，格式如下（純文字，可用簡單的 Markdown 粗體與項目符號，不要用標題 # 語法）：

**🌍 地球科學日報｜{{今天日期}}**

**🌋 地球物理與地震學**
- (每則一行重點說明 + 來源連結)

**🔭 其他地球科學領域**
- (每則一行重點說明 + 來源連結，若無則寫「今日無重大更新」)

候選項目清單：
{candidate_block}
"""


def call_gemini(prompt: str) -> str:
    api_key = os.environ["GOOGLE_API_KEY"]
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 1500},
    }
    resp = requests.post(
        GEMINI_URL,
        params={"key": api_key},
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def post_discord(message: str) -> None:
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    chunk_size = 1900
    chunks = [message[i : i + chunk_size] for i in range(0, len(message), chunk_size)] or [message]
    for chunk in chunks:
        resp = requests.post(webhook_url, json={"content": chunk}, timeout=30)
        resp.raise_for_status()


def main() -> None:
    candidates = gather_candidates()
    today = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    if not candidates:
        post_discord(f"**🌍 地球科學日報｜{today}**\n\n今日各來源皆無新內容，明天再見！")
        return

    prompt = build_prompt(candidates).replace("{今天日期}", today)
    digest = call_gemini(prompt)
    post_discord(digest)
    mark_seen(candidates)


if __name__ == "__main__":
    main()
