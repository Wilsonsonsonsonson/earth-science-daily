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
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = REPO_ROOT / "data" / "seen.json"
PREFS_FILE = REPO_ROOT / "preferences.json"

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
)

# 來源白名單：只收錄權威機構（AGU、EGU/Copernicus、Nature、arXiv、USGS、EMSC、
# Smithsonian）。系統只從這份清單抓取，掠奪性期刊無法進入日報。
# 新增來源前請先確認出版方信譽（可查 DOAJ 收錄狀態與出版社是否為 OASPA/COPE 成員）。
# category: "seismo" = 地球物理與地震學 (主要焦點), "other" = 其他地球科學領域
# peer_reviewed: False 的來源（如 arXiv 預印本）會在日報中明確標註未經同儕審查
RSS_SOURCES = [
    {"name": "arXiv physics.geo-ph (地球物理預印本)", "url": "https://rss.arxiv.org/rss/physics.geo-ph", "category": "seismo", "peer_reviewed": False},
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
                "peer_reviewed": source.get("peer_reviewed", True),
                "title": entry.get("title", "(無標題)").strip(),
                "link": entry.get("link", ""),
                "summary": (entry.get("summary", "") or "")[:400],
                "published": published.isoformat(),
            }
        )
    return items


def load_prefs() -> dict:
    if PREFS_FILE.exists():
        prefs = json.loads(PREFS_FILE.read_text())
        return {k: v for k, v in prefs.items() if not k.startswith("_")}
    return {}


def apply_prefs(candidates: list, prefs: dict) -> list:
    blocked_sources = set(prefs.get("blocked_sources", []))
    blocked_keywords = [k.lower() for k in prefs.get("blocked_keywords", [])]
    kept = []
    for c in candidates:
        if c["source"] in blocked_sources:
            continue
        haystack = f"{c['title']} {c['summary']}".lower()
        if any(k in haystack for k in blocked_keywords):
            continue
        kept.append(c)
    return kept


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


def build_prompt(candidates: list, today: str, prefs: dict) -> str:
    lines = []
    for c in candidates:
        review_tag = "" if c.get("peer_reviewed", True) else "【預印本，未經同儕審查】"
        lines.append(
            f"- [{c['category']}]{review_tag} 來源：{c['source']} | 標題：{c['title']} | "
            f"時間：{c['published']} | 連結：{c['link']} | 摘要：{c['summary']}"
        )
    candidate_block = "\n".join(lines) if lines else "(今日無新候選項目)"

    guidelines = prefs.get("editorial_guidelines", [])
    guidelines_block = ""
    if guidelines:
        rules = "\n".join(f"- {g}" for g in guidelines)
        guidelines_block = f"""
讀者自訂的品質守則（僅作為品質過濾標準，不是主題偏好——你仍必須維持領域與主題的多樣性，不可因此讓日報內容窄化到單一主題）：
{rules}
"""

    return f"""你是一份地球科學日報的主編，讀者是對地球科學有興趣的一般讀者，會在吃早餐時滑手機瀏覽。今天是 {today}。

你的任務：從下面的候選項目清單中，篩選出「真正值得一看」的內容，其餘全部丟棄。候選項目分兩類標籤：
- [seismo]：地球物理與地震學，這是本日報的主要焦點，請優先且較寬鬆地納入（例如規模較大或有感地震、重要新論文、火山活動明顯變化）。最多挑 8 則。
- [other]：其他地球科學領域（地質、大氣、海洋、行星科學等），作為次要補充，只挑真正有意思或重要的內容，最多 3 則，不必每天都有。

篩選與寫作原則：
- 忽略例行、重複、規模很小或無明顯新聞價值的項目；同一事件被多個來源報導時合併成一則，選最權威的連結。
- 多起地震可以合併成一則「今日地震動態」總覽，把最大或最值得注意的一兩起講清楚（規模、地點、深度、是否近人口稠密區），其餘一句帶過。
- 每則的 title 要短而有力（20 字以內），summary 用一句話講出「為什麼值得看」而不是复述標題（60 字以內），語氣自然、像懂行的朋友報消息，不聳動、不誇大。
- 學術論文要把重點翻成一般讀者聽得懂的話，避免直譯術語堆疊。
- 標註【預印本，未經同儕審查】的項目：入選標準從嚴，且入選後 source 欄位必須寫「arXiv 預印本（未經同儕審查）」，讓讀者知道其結論尚未定案。
- intro 是 2~3 句的今日導言，點出今天最大亮點，語氣輕鬆但專業，像早報編輯的開場白。
- 全部使用繁體中文。emoji 為每則挑一個貼切的（如 🌋 火山、📄 論文、🌊 海嘯、📡 觀測技術、🧊 冰凍圈、🪐 行星）。
{guidelines_block}
請嚴格輸出以下 JSON 格式（不要加任何其他文字或 markdown 圍欄）。source 欄位填該則內容的來源名稱（期刊名／機構名，合併多來源時填最權威的那個）：
{{
  "intro": "今日導言，2~3 句",
  "seismo": [
    {{"emoji": "🌋", "title": "短標題", "summary": "一句話重點", "link": "https://...", "source": "來源名稱"}}
  ],
  "other": [
    {{"emoji": "📄", "title": "短標題", "summary": "一句話重點", "link": "https://...", "source": "來源名稱"}}
  ]
}}

若某類別今天沒有值得報導的內容，該欄位給空陣列 []。

候選項目清單：
{candidate_block}
"""


def call_gemini(prompt: str) -> dict:
    api_key = os.environ["GOOGLE_API_KEY"]
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 2000,
            "responseMimeType": "application/json",
        },
    }
    resp = requests.post(
        GEMINI_URL,
        params={"key": api_key},
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return json.loads(text)


COLOR_HEADER = 0x1ABC9C   # 湖水綠：導言卡
COLOR_SEISMO = 0xE74C3C   # 紅：地球物理與地震學
COLOR_OTHER = 0x3498DB    # 藍：其他領域

WEEKDAYS_ZH = ["一", "二", "三", "四", "五", "六", "日"]


def item_field(item: dict) -> dict:
    title = f"{item.get('emoji', '•')} {item['title']}"
    summary = item.get("summary", "").strip()
    link = item.get("link", "")
    source = item.get("source", "").strip()
    value = summary
    tail = []
    if source:
        tail.append(f"來源：{source}")
    if link:
        tail.append(f"[閱讀原文 →]({link})")
    if tail:
        value += "\n" + " · ".join(tail)
    return {"name": title[:256], "value": value[:1024] or "（見連結）", "inline": False}


def section_embed(title: str, items: list, color: int, empty_text: str) -> dict:
    embed = {"title": title, "color": color}
    if items:
        embed["fields"] = [item_field(i) for i in items[:10]]
    else:
        embed["description"] = empty_text
    return embed


def build_embeds(digest: dict, now_tw: datetime, stats: dict) -> list:
    weekday = WEEKDAYS_ZH[now_tw.weekday()]
    header = {
        "title": f"🌍 地球科學日報",
        "description": digest.get("intro", "早安！以下是過去 24 小時的地球科學精選。"),
        "color": COLOR_HEADER,
        "author": {"name": f"{now_tw.strftime('%Y 年 %m 月 %d 日')}（週{weekday}）早報"},
    }
    seismo = section_embed(
        "🌋 地球物理與地震學",
        digest.get("seismo", []),
        COLOR_SEISMO,
        "今日無重大更新，地球很平靜。",
    )
    other = section_embed(
        "🔭 其他地球科學",
        digest.get("other", []),
        COLOR_OTHER,
        "今日無重大更新。",
    )
    other["footer"] = {
        "text": (
            f"掃描 {stats['sources']} 個來源 · {stats['candidates']} 則候選 · "
            f"精選 {stats['picked']} 則 · 由 Gemini 整理"
        )
    }
    other["timestamp"] = datetime.now(timezone.utc).isoformat()
    return [header, seismo, other]


def post_discord_embeds(embeds: list) -> None:
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    payload = {
        "username": "地球科學日報",
        "avatar_url": "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/1f30d.png",
        "embeds": embeds,
    }
    resp = requests.post(webhook_url, json=payload, timeout=30)
    resp.raise_for_status()


def post_discord_text(message: str) -> None:
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    chunk_size = 1900
    chunks = [message[i : i + chunk_size] for i in range(0, len(message), chunk_size)] or [message]
    for chunk in chunks:
        resp = requests.post(webhook_url, json={"content": chunk}, timeout=30)
        resp.raise_for_status()


def main() -> None:
    prefs = load_prefs()
    fresh = gather_candidates()
    candidates = apply_prefs(fresh, prefs)
    now_tw = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    today = now_tw.strftime("%Y-%m-%d")

    if not candidates:
        post_discord_text(f"**🌍 地球科學日報｜{today}**\n\n今日各來源皆無新內容，明天再見！")
        mark_seen(fresh)
        return

    prompt = build_prompt(candidates, today, prefs)
    digest = call_gemini(prompt)
    stats = {
        "sources": len(RSS_SOURCES) + 2,  # +2: USGS 與 EMSC API
        "candidates": len(candidates),
        "picked": len(digest.get("seismo", [])) + len(digest.get("other", [])),
    }
    post_discord_embeds(build_embeds(digest, now_tw, stats))
    mark_seen(fresh)


if __name__ == "__main__":
    main()
