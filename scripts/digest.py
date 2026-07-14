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
WORDS_FILE = REPO_ROOT / "data" / "words.json"
ARCHIVE_DIR = REPO_ROOT / "archive"
PREFS_FILE = REPO_ROOT / "preferences.json"

# 依序嘗試：模型忙碌（503）或對新用戶關閉（404）時自動退到下一個
GEMINI_MODELS = ["gemini-flash-latest", "gemini-flash-lite-latest", "gemini-2.5-flash-lite"]
GEMINI_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

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

# 台灣地震專區：預設用 USGS 台灣周邊範圍（免金鑰）；若設定了 CWA_API_KEY
# （中央氣象署 Open Data 授權碼，https://opendata.cwa.gov.tw 免費申請），
# 改用氣象署「顯著有感地震報告」，含各地震度等更完整資訊。
USGS_QUERY_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
TAIWAN_BBOX = {"minlatitude": 21, "maxlatitude": 26.5, "minlongitude": 118.5, "maxlongitude": 123.5}
TAIWAN_MIN_MAG = 4.0
CWA_URL = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/E-A0015-001"

# 全球颱風動態：JMA（西太平洋，對台灣最重要）+ NHC（大西洋/東太平洋）+ JTWC（補其他海域）
JMA_TC_LIST_URL = "https://www.jma.go.jp/bosai/typhoon/data/targetTc.json"
JMA_TC_SPEC_URL = "https://www.jma.go.jp/bosai/typhoon/data/{tc}/specifications.json"
JMA_TYPHOON_PAGE = "https://www.jma.go.jp/bosai/map.html#contents=typhoon"
NHC_STORMS_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"
JTWC_RSS_URL = "https://www.metoc.navy.mil/jtwc/rss/jtwc.rss"


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


def load_words() -> list:
    if WORDS_FILE.exists():
        return json.loads(WORDS_FILE.read_text())
    return []


def save_word(entry: dict, today: str) -> None:
    words = load_words()
    words.append({"date": today, **entry})
    WORDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    WORDS_FILE.write_text(json.dumps(words[-365:], ensure_ascii=False, indent=2))


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


def fetch_taiwan_cwa(now: datetime) -> list:
    api_key = os.environ.get("CWA_API_KEY", "")
    if not api_key:
        return []
    items = []
    try:
        resp = requests.get(
            CWA_URL,
            params={"Authorization": api_key, "limit": 10},
            timeout=20,
        )
        resp.raise_for_status()
        quakes = resp.json()["records"]["Earthquake"]
    except Exception as exc:
        print(f"[warn] CWA fetch failed, falling back to USGS bbox: {exc}", file=sys.stderr)
        return []

    for q in quakes:
        try:
            info = q["EarthquakeInfo"]
            origin = datetime.fromisoformat(info["OriginTime"]).astimezone(timezone.utc)
            if not within_lookback(origin, now):
                continue
            mag = info["EarthquakeMagnitude"]["MagnitudeValue"]
            items.append(
                {
                    "source": "中央氣象署顯著有感地震報告",
                    "category": "taiwan",
                    "peer_reviewed": True,
                    "title": f"M{mag} {info['Epicenter']['Location']}",
                    "link": q.get("Web", "https://scweb.cwa.gov.tw/"),
                    "summary": (
                        f"規模 {mag}，深度 {info['FocalDepth']} 公里。"
                        f"{q.get('ReportContent', '')}"
                    )[:400],
                    "published": origin.isoformat(),
                }
            )
        except (KeyError, ValueError) as exc:
            print(f"[warn] skipping malformed CWA record: {exc}", file=sys.stderr)
    return items


def fetch_taiwan_usgs(now: datetime) -> list:
    items = []
    params = {
        "format": "geojson",
        "starttime": (now - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%S"),
        "minmagnitude": TAIWAN_MIN_MAG,
        **TAIWAN_BBOX,
    }
    try:
        resp = requests.get(USGS_QUERY_URL, params=params, timeout=20)
        resp.raise_for_status()
        features = resp.json().get("features", [])
    except Exception as exc:
        print(f"[warn] failed to fetch USGS Taiwan query: {exc}", file=sys.stderr)
        return items

    for feature in features:
        props = feature.get("properties", {})
        ts_ms = props.get("time")
        published = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms else now
        items.append(
            {
                "source": "USGS（台灣周邊 M4.0+）",
                "category": "taiwan",
                "peer_reviewed": True,
                "title": props.get("title", "(無標題)"),
                "link": props.get("url", ""),
                "summary": f"規模 {props.get('mag')}，地點：{props.get('place')}",
                "published": published.isoformat(),
            }
        )
    return items


def fetch_taiwan(now: datetime) -> list:
    return fetch_taiwan_cwa(now) or fetch_taiwan_usgs(now)


def _strip_html(text: str) -> str:
    import re

    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def fetch_typhoons(now: datetime) -> list:
    """Active tropical cyclones worldwide. Status updates recur daily, so items
    are deduped by (system, date) instead of by link."""
    items = []
    date_tag = now.strftime("%Y-%m-%d")

    # JMA：西太平洋現役颱風／熱帶性低氣壓，結構化實況
    try:
        tcs = requests.get(JMA_TC_LIST_URL, timeout=20).json()
        for tc in tcs:
            tc_id = tc.get("tropicalCyclone", "")
            try:
                spec = requests.get(JMA_TC_SPEC_URL.format(tc=tc_id), timeout=20).json()
                title_part = spec[0]
                analysis = spec[1] if len(spec) > 1 else {}
                name_en = (title_part.get("name") or {}).get("en") or "（未命名）"
                num = title_part.get("typhoonNumber", "")
                cat = (title_part.get("category") or {}).get("en", "")
                wind_kt = ((analysis.get("maximumWind") or {}).get("sustained") or {}).get("kt", "?")
                pressure = analysis.get("pressure", "?")
                pos = (analysis.get("position") or {}).get("deg", ["?", "?"])
                course = analysis.get("course", "?")
                speed = (analysis.get("speed") or {}).get("km/h", "?")
                location = analysis.get("location", "")
                items.append(
                    {
                        "source": "日本氣象廳（JMA）颱風情報",
                        "category": "typhoon",
                        "peer_reviewed": True,
                        "title": f"颱風 {name_en}（編號 {num}，國際分類 {cat}）",
                        "link": JMA_TYPHOON_PAGE,
                        "dedupe_key": f"jma-{tc_id}-{date_tag}",
                        "summary": (
                            f"中心位置 北緯{pos[0]}度、東經{pos[1]}度（{location}），中心氣壓 {pressure} hPa，"
                            f"最大持續風速 {wind_kt} 節，向{course}移動 時速{speed}公里"
                        ),
                        "published": now.isoformat(),
                    }
                )
            except Exception as exc:
                print(f"[warn] JMA TC {tc_id} detail failed: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"[warn] JMA typhoon list failed: {exc}", file=sys.stderr)

    # NHC：大西洋／東太平洋現役系統
    try:
        storms = requests.get(NHC_STORMS_URL, timeout=20).json().get("activeStorms", [])
        for s in storms:
            items.append(
                {
                    "source": "美國國家颶風中心（NHC）",
                    "category": "typhoon",
                    "peer_reviewed": True,
                    "title": f"{s.get('classification', '')} {s.get('name', '')}（{s.get('binNumber', '')}）",
                    "link": "https://www.nhc.noaa.gov/",
                    "dedupe_key": f"nhc-{s.get('id', s.get('name', ''))}-{date_tag}",
                    "summary": (
                        f"位置 {s.get('latitudeNumeric', '?')}, {s.get('longitudeNumeric', '?')}，"
                        f"強度 {s.get('intensity', '?')} 節，氣壓 {s.get('pressure', '?')} mb，"
                        f"移動 {s.get('movementDir', '?')}° / {s.get('movementSpeed', '?')} 節"
                    ),
                    "published": now.isoformat(),
                }
            )
    except Exception as exc:
        print(f"[warn] NHC storms failed: {exc}", file=sys.stderr)

    # JTWC：補其他海域（南半球、印度洋）；原始警報文字，交給 AI 摘要
    try:
        parsed = feedparser.parse(JTWC_RSS_URL)
        for i, entry in enumerate(parsed.entries):
            desc = _strip_html(entry.get("summary", ""))
            if not desc or desc.lower().startswith("no current"):
                continue
            items.append(
                {
                    "source": "美軍聯合颱風警報中心（JTWC）",
                    "category": "typhoon",
                    "peer_reviewed": True,
                    "title": entry.get("title", "JTWC 警報"),
                    "link": "https://www.metoc.navy.mil/jtwc/jtwc.html",
                    "dedupe_key": f"jtwc-{i}-{date_tag}",
                    "summary": desc[:400],
                    "published": now.isoformat(),
                }
            )
    except Exception as exc:
        print(f"[warn] JTWC feed failed: {exc}", file=sys.stderr)

    return items


def gather_candidates() -> list:
    """Return new-since-last-run items. Does NOT persist yet — call mark_seen()
    only after the digest has been posted successfully, so a failed run doesn't
    silently drop items."""
    now = datetime.now(timezone.utc)
    seen = load_seen()
    candidates = []

    # 台灣專區優先收集；全球來源中重複的同一起地震會被連結去重跳過
    candidates.extend(fetch_taiwan(now))
    candidates.extend(fetch_typhoons(now))
    for source in RSS_SOURCES:
        candidates.extend(fetch_rss(source, now))
    candidates.extend(fetch_usgs(now))
    candidates.extend(fetch_seismicportal(now))

    fresh, keys = [], set()
    for c in candidates:
        key = c.get("dedupe_key") or c["link"]
        if key and key not in seen and key not in keys:
            fresh.append(c)
            keys.add(key)
    return fresh


def mark_seen(candidates: list) -> None:
    now = datetime.now(timezone.utc)
    seen = load_seen()
    for c in candidates:
        seen[c.get("dedupe_key") or c["link"]] = now.isoformat()
    save_seen(seen)


def build_prompt(candidates: list, today: str, prefs: dict, taught_words: list) -> str:
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

    taught_block = "、".join(w["term"] for w in taught_words[-90:]) or "（尚無）"

    terminology = prefs.get("terminology", [])
    terminology_block = ""
    if terminology:
        pairs = "\n".join(f"- {t}" for t in terminology)
        terminology_block = f"""
中文一律使用台灣學術界慣用語（繁體中文、台灣譯名），嚴禁使用中國大陸的學術用語。讀者是台灣的地球科學學習者，錯誤用語會養壞他的專業語感。常見對照（左為英文，右為台灣正確用法）：
{pairs}
不在對照表內的詞彙，也一律優先選擇台灣學界與國家教育研究院雙語詞彙資料庫的慣用譯名。
"""

    return f"""你是一份地球科學日報的主編，讀者是對地球科學有興趣的一般讀者，會在吃早餐時滑手機瀏覽。今天是 {today}。

你的任務：從下面的候選項目清單中，篩選出「真正值得一看」的內容，其餘全部丟棄。候選項目分四類標籤：
- [taiwan]：台灣及周邊的地震，讀者在台灣、對此最關心，全部納入 taiwan 陣列（除非明顯是同一起地震的重複報告）。
- [typhoon]：全球現役熱帶氣旋動態。同一個颱風被多個機構（JMA/NHC/JTWC）報告時合併成一則，以 JMA 資料為準（西太平洋）。每則講清楚：名稱與編號、目前強度與位置、移動方向速度、未來趨勢，以及對台灣或鄰近地區的潛在影響（若在西太平洋）。颱風強度用台灣中央氣象署的分級稱呼（輕度颱風＝TS/STS、中度颱風＝TY、強烈颱風），並在括號附國際分類。JTWC 的原始警報文字要消化成人話。全球都無活躍系統時給空陣列。
- [seismo]：地球物理與地震學，這是本日報的主要焦點，請優先且較寬鬆地納入（例如規模較大或有感地震、重要新論文、火山活動明顯變化）。最多挑 8 則。
- [other]：其他地球科學領域（地質、大氣、海洋、行星科學等），作為次要補充，只挑真正有意思或重要的內容，最多 3 則，不必每天都有。

篩選與寫作原則：
- 忽略例行、重複、規模很小或無明顯新聞價值的項目；同一事件被多個來源報導時合併成一則，選最權威的連結。
- 多起地震可以合併成一則「今日地震動態」總覽，把最大或最值得注意的一兩起講清楚（規模、地點、深度、是否近人口稠密區），其餘一句帶過。
- 日報是雙語格式（英文在前、繁體中文在後），讀者想藉此練習專業英語，也想「每天真的學到東西」，風格參考台灣的泛科學（PanSci）：有趣、有料、把科學脈絡講清楚，不是乾巴巴的事件通報：
  - title_en：精煉的英文標題（期刊論文可直接用原標題或縮短版，事件類自己寫，10 個單字以內）。
  - summary_en：一到兩句自然、道地的英文摘要（35 個單字以內），用學術新聞的語感，是讀者的英語教材，不要是中式英文。
  - title_zh：中文短標題（20 字以內）。
  - summary_zh：中文 2~3 句（120 字以內），講清楚「發生什麼＋所以呢？」——重點放在意義與影響，不是 summary_en 的直譯，語氣像懂行的朋友報消息，不聳動、不誇大。
  - note_zh：1~2 句科普補充（80 字以內）——這則背後的科學原理、學界原本的認知、或一個讀者可能不知道的背景知識。這是讀者「學到東西」的關鍵欄位，寧可講一個原理講透，不要堆砌名詞。沒有適合的補充時可給空字串。
- 學術論文要把重點翻成一般讀者聽得懂的話，避免直譯術語堆疊。
- deep_dive（今日深度導讀）：從所有入選項目中挑「今天最有意思、最值得深入理解」的一則（優先挑地球物理／地震學），寫一篇 250~350 字的迷你科普文（body_zh）。結構：用一個生活化的比喻、問題或場景開場 → 交代背景（這領域原本知道什麼／缺什麼）→ 講清楚新發現或事件本身 → 為什麼重要、對誰有影響 → 結尾留一個 fun fact 或思考點。段落間用換行分隔，語氣像泛科學的文章，專業但不掉書袋。
- 標註【預印本，未經同儕審查】的項目：入選標準從嚴，且入選後 source 欄位必須寫「arXiv 預印本（未經同儕審查）」，讓讀者知道其結論尚未定案。
- intro_en 是 1~2 句英文的今日導言；intro_zh 是 2~3 句中文導言，點出今天最大亮點，語氣輕鬆但專業，像早報編輯的開場白（兩者是同一個意思的兩種表達，不必逐字對譯）。
- emoji 為每則挑一個貼切的（如 🌋 火山、📄 論文、🌊 海嘯、📡 觀測技術、🧊 冰凍圈、🪐 行星）。
{terminology_block}{guidelines_block}
另外，請從今天入選的內容中挑一個對學習者最有價值的地球科學專業英語術語，做成「每日一詞」：
- 優先挑今天內容中實際出現、且對讀地科論文常用的詞（如 attenuation、subduction、aseismic）。
- definition_en 用簡單英文解釋（給非母語者），example_en 是一句自然的學術例句（最好呼應今天的新聞），zh 是台灣學界譯名，note 是一句中文記憶點或詞源小知識。
- 這些詞已經教過，不要重複：{taught_block}

請嚴格輸出以下 JSON 格式（不要加任何其他文字或 markdown 圍欄）。source 欄位填該則內容的來源名稱（期刊名／機構名，合併多來源時填最權威的那個）：
{{
  "intro_en": "One or two sentences in English",
  "intro_zh": "今日導言，2~3 句",
  "taiwan": [
    {{"emoji": "🚨", "title_en": "Short English title", "summary_en": "One or two English sentences", "title_zh": "中文短標題", "summary_zh": "中文2~3句：發生什麼＋所以呢", "note_zh": "1~2句科普補充（可為空字串）", "link": "https://...", "source": "來源名稱"}}
  ],
  "typhoon": [
    {{"emoji": "🌀", "title_en": "Short English title", "summary_en": "One or two English sentences", "title_zh": "中文短標題", "summary_zh": "中文2~3句：現況＋趨勢＋影響", "note_zh": "1~2句科普補充（可為空字串）", "link": "https://...", "source": "來源名稱"}}
  ],
  "seismo": [
    {{"emoji": "🌋", "title_en": "Short English title", "summary_en": "One or two English sentences", "title_zh": "中文短標題", "summary_zh": "中文2~3句：發生什麼＋所以呢", "note_zh": "1~2句科普補充（可為空字串）", "link": "https://...", "source": "來源名稱"}}
  ],
  "other": [
    {{"emoji": "📄", "title_en": "Short English title", "summary_en": "One or two English sentences", "title_zh": "中文短標題", "summary_zh": "中文2~3句：發生什麼＋所以呢", "note_zh": "1~2句科普補充（可為空字串）", "link": "https://...", "source": "來源名稱"}}
  ],
  "deep_dive": {{"emoji": "🔬", "title_zh": "深度導讀標題", "title_en": "English title", "body_zh": "250~350字迷你科普文，段落用\\n分隔", "link": "https://...", "source": "來源名稱"}},
  "word_of_the_day": {{"term": "attenuation", "pos": "n.", "definition_en": "Simple English definition", "example_en": "A natural academic example sentence.", "zh": "衰減", "note": "一句中文記憶點"}}
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
            "maxOutputTokens": 6000,
            "responseMimeType": "application/json",
        },
    }
    last_error: Exception = RuntimeError("no Gemini model attempted")
    for model in GEMINI_MODELS:
        for attempt in range(2):
            try:
                resp = requests.post(
                    GEMINI_URL_TMPL.format(model=model),
                    params={"key": api_key},
                    json=payload,
                    timeout=120,
                )
                resp.raise_for_status()
                parts = resp.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts).strip()
                if text.startswith("```"):
                    text = text.strip("`").removeprefix("json").strip()
                return json.loads(text)
            except Exception as exc:
                last_error = exc
                print(f"[warn] {model} attempt {attempt + 1} failed: {exc}", file=sys.stderr)
                time.sleep(15)
    raise last_error


COLOR_HEADER = 0x1ABC9C   # 湖水綠：導言卡
COLOR_TAIWAN = 0xF39C12   # 琥珀：台灣地震動態
COLOR_TYPHOON = 0x00BCD4  # 青：全球颱風動態
COLOR_SEISMO = 0xE74C3C   # 紅：地球物理與地震學
COLOR_OTHER = 0x3498DB    # 藍：其他領域
COLOR_DIVE = 0x27AE60     # 綠：今日深度導讀
COLOR_WORD = 0x9B59B6     # 紫：每日一詞
COLOR_WEEKLY = 0x8E44AD   # 深紫：週日回顧

WEEKDAYS_ZH = ["一", "二", "三", "四", "五", "六", "日"]


def item_field(item: dict) -> dict:
    title_en = item.get("title_en") or item.get("title", "")
    name = f"{item.get('emoji', '•')} {title_en}"

    lines = []
    summary_en = (item.get("summary_en") or "").strip()
    if summary_en:
        lines.append(summary_en)
    title_zh = (item.get("title_zh") or "").strip()
    summary_zh = (item.get("summary_zh") or item.get("summary") or "").strip()
    zh = "｜".join(x for x in (title_zh, summary_zh) if x)
    if zh:
        lines.append(f"🇹🇼 {zh}")
    note_zh = (item.get("note_zh") or "").strip()
    if note_zh:
        lines.append(f"💡 {note_zh}")

    tail = []
    source = (item.get("source") or "").strip()
    if source:
        tail.append(f"來源：{source}")
    link = item.get("link", "")
    if link:
        tail.append(f"[閱讀原文 →]({link})")
    if tail:
        lines.append(" · ".join(tail))

    value = "\n".join(lines)
    return {"name": name[:256], "value": value[:1024] or "（見連結）", "inline": False}


def section_embed(title: str, items: list, color: int, empty_text: str) -> dict:
    embed = {"title": title, "color": color}
    if items:
        embed["fields"] = [item_field(i) for i in items[:10]]
    else:
        embed["description"] = empty_text
    return embed


def build_embeds(digest: dict, now_tw: datetime, stats: dict) -> list:
    weekday = WEEKDAYS_ZH[now_tw.weekday()]
    intro_en = (digest.get("intro_en") or "").strip()
    intro_zh = (digest.get("intro_zh") or digest.get("intro") or "早安！以下是過去 24 小時的地球科學精選。").strip()
    description = f"*{intro_en}*\n\n{intro_zh}" if intro_en else intro_zh
    header = {
        "title": f"🌍 地球科學日報 · Earth Science Daily",
        "description": description,
        "color": COLOR_HEADER,
        "author": {"name": f"{now_tw.strftime('%Y 年 %m 月 %d 日')}（週{weekday}）早報"},
    }
    taiwan = section_embed(
        "🇹🇼 台灣地震動態",
        digest.get("taiwan", []),
        COLOR_TAIWAN,
        "過去 24 小時台灣及周邊無規模 4 以上地震。",
    )
    typhoon = section_embed(
        "🌀 全球颱風動態",
        digest.get("typhoon", []),
        COLOR_TYPHOON,
        "目前全球無活躍的熱帶氣旋。",
    )
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
    embeds = [header, taiwan, typhoon, seismo, other]

    dive = digest.get("deep_dive") or {}
    if dive.get("body_zh"):
        body = dive["body_zh"].strip()
        tail = []
        if dive.get("source"):
            tail.append(f"來源：{dive['source']}")
        if dive.get("link"):
            tail.append(f"[閱讀原文 →]({dive['link']})")
        if tail:
            body += "\n\n" + " · ".join(tail)
        title_en = (dive.get("title_en") or "").strip()
        title = f"{dive.get('emoji', '🔬')} 今日深度導讀：{dive.get('title_zh', '')}"
        if title_en:
            body = f"*{title_en}*\n\n{body}"
        embeds.append(
            {"title": title[:256], "description": body[:4096], "color": COLOR_DIVE}
        )

    word = digest.get("word_of_the_day") or {}
    if word.get("term"):
        zh = word.get("zh", "")
        pos = word.get("pos", "")
        lines = [word.get("definition_en", "").strip()]
        example = word.get("example_en", "").strip()
        if example:
            lines.append(f"> *{example}*")
        note = word.get("note", "").strip()
        if note:
            lines.append(f"🇹🇼 **{zh}**｜{note}")
        elif zh:
            lines.append(f"🇹🇼 **{zh}**")
        embeds.append(
            {
                "title": f"📖 每日一詞：{word['term']}" + (f" ({pos})" if pos else ""),
                "description": "\n".join(l for l in lines if l)[:4096],
                "color": COLOR_WORD,
            }
        )

    embeds[-1]["footer"] = {
        "text": (
            f"掃描 {stats['sources']} 個來源 · {stats['candidates']} 則候選 · "
            f"精選 {stats['picked']} 則 · 由 Gemini 整理"
        )
    }
    embeds[-1]["timestamp"] = datetime.now(timezone.utc).isoformat()
    return embeds


def embed_size(embed: dict) -> int:
    total = len(embed.get("title", "")) + len(embed.get("description", ""))
    total += len(embed.get("author", {}).get("name", ""))
    total += len(embed.get("footer", {}).get("text", ""))
    for f in embed.get("fields", []):
        total += len(f.get("name", "")) + len(f.get("value", ""))
    return total


def post_discord_embeds(embeds: list) -> None:
    # Discord 限制：單則訊息所有 embed 字元合計 ≤6000、embed 數 ≤10，超過就分批送
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    batches, batch, size = [], [], 0
    for e in embeds:
        s = embed_size(e)
        if batch and (size + s > 5500 or len(batch) >= 10):
            batches.append(batch)
            batch, size = [], 0
        batch.append(e)
        size += s
    if batch:
        batches.append(batch)

    for b in batches:
        payload = {
            "username": "地球科學日報",
            "avatar_url": "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/1f30d.png",
            "embeds": b,
        }
        resp = requests.post(webhook_url, json=payload, timeout=30)
        resp.raise_for_status()


def render_archive_md(digest: dict, now_tw: datetime) -> str:
    weekday = WEEKDAYS_ZH[now_tw.weekday()]
    lines = [
        "---",
        f"title: 地球科學日報 {now_tw.strftime('%Y-%m-%d')}",
        "---",
        "",
        f"# 🌍 地球科學日報 {now_tw.strftime('%Y-%m-%d')}（週{weekday}）",
        "",
    ]
    intro_en = (digest.get("intro_en") or "").strip()
    intro_zh = (digest.get("intro_zh") or "").strip()
    if intro_en:
        lines += [f"> *{intro_en}*", ">"]
    if intro_zh:
        lines += [f"> {intro_zh}"]
    lines.append("")

    sections = [
        ("🇹🇼 台灣地震動態", "taiwan"),
        ("🌀 全球颱風動態", "typhoon"),
        ("🌋 地球物理與地震學", "seismo"),
        ("🔭 其他地球科學", "other"),
    ]
    for heading, key in sections:
        items = digest.get(key, [])
        lines.append(f"## {heading}")
        lines.append("")
        if not items:
            lines += ["今日無重大更新。", ""]
            continue
        for it in items:
            title_en = it.get("title_en", "")
            link = it.get("link", "")
            title_line = f"[{title_en}]({link})" if link else title_en
            lines.append(f"- {it.get('emoji', '•')} **{title_line}**")
            if it.get("summary_en"):
                lines.append(f"  {it['summary_en']}")
            zh = "｜".join(x for x in (it.get("title_zh", ""), it.get("summary_zh", "")) if x)
            if zh:
                lines.append(f"  🇹🇼 {zh}")
            if it.get("note_zh"):
                lines.append(f"  💡 {it['note_zh']}")
            if it.get("source"):
                lines.append(f"  <sub>來源：{it['source']}</sub>")
        lines.append("")

    dive = digest.get("deep_dive") or {}
    if dive.get("body_zh"):
        lines += [
            f"## {dive.get('emoji', '🔬')} 今日深度導讀：{dive.get('title_zh', '')}",
            "",
        ]
        if dive.get("title_en"):
            lines += [f"*{dive['title_en']}*", ""]
        lines += [dive["body_zh"], ""]
        if dive.get("link"):
            lines += [f"[閱讀原文]({dive['link']})（來源：{dive.get('source', '')}）", ""]

    word = digest.get("word_of_the_day") or {}
    if word.get("term"):
        lines += [
            "## 📖 每日一詞",
            "",
            f"**{word['term']}** ({word.get('pos', '')}) — {word.get('zh', '')}",
            "",
            word.get("definition_en", ""),
            "",
            f"> *{word.get('example_en', '')}*",
            "",
            word.get("note", ""),
            "",
        ]
    return "\n".join(lines)


def write_archive(digest: dict, now_tw: datetime) -> Path:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    day_file = ARCHIVE_DIR / f"{now_tw.strftime('%Y-%m-%d')}.md"
    day_file.write_text(render_archive_md(digest, now_tw))

    index = ARCHIVE_DIR / "index.md"
    entries = sorted(
        (p.stem for p in ARCHIVE_DIR.glob("????-??-??.md")), reverse=True
    )
    index_lines = ["---", "title: 日報歸檔", "---", "", "# 📚 地球科學日報歸檔", ""]
    index_lines += [f"- [{d}]({d}.md)" for d in entries]
    index.write_text("\n".join(index_lines) + "\n")
    return day_file


def build_weekly_prompt(archive_texts: list, today: str) -> str:
    joined = "\n\n---\n\n".join(archive_texts)
    return f"""你是地球科學週報的主編。今天是 {today}（週日）。以下是過去一週每天的日報內容，請從中挑出本週最重要、最值得回味的 3~5 則，寫成週日回顧特刊。

要求：
- 用繁體中文與台灣學術用語（mantle→地函、magnitude→規模等）。
- week_summary_zh：3~4 句的本週總評，點出本週地球科學的大脈絡（例如某地震序列的發展、某研究方向的多篇進展）。
- highlights：每則含 emoji、中文短標題（20 字內）、why_zh（一句話說明為何本週回顧它值得再看一次，可以補充當時沒說的後續脈絡）、link（沿用日報中的連結）。
- 不要逐日流水帳，要有「回顧視角」——把一週的點連成線。

請嚴格輸出以下 JSON（不要加任何其他文字）：
{{
  "week_summary_zh": "本週總評",
  "highlights": [
    {{"emoji": "🌏", "title_zh": "中文短標題", "why_zh": "一句話", "link": "https://..."}}
  ]
}}

過去一週的日報：
{joined}
"""


def build_weekly_embeds(weekly: dict, words: list, now_tw: datetime) -> list:
    header = {
        "title": "📚 週日回顧特刊 · Weekly Review",
        "description": weekly.get("week_summary_zh", ""),
        "color": COLOR_WEEKLY,
        "author": {"name": f"{now_tw.strftime('%Y 年 %m 月 %d 日')} 本週回顧"},
    }
    highlight_fields = []
    for h in weekly.get("highlights", [])[:8]:
        value = h.get("why_zh", "")
        if h.get("link"):
            value += f"\n[回顧原文 →]({h['link']})"
        highlight_fields.append(
            {"name": f"{h.get('emoji', '•')} {h.get('title_zh', '')}"[:256], "value": value[:1024], "inline": False}
        )
    highlights = {"title": "⭐ 本週精選回顧", "color": COLOR_WEEKLY, "fields": highlight_fields or None}
    if not highlight_fields:
        highlights.pop("fields")
        highlights["description"] = "本週較平靜，沒有特別需要回顧的內容。"

    embeds = [header, highlights]

    week_ago = (now_tw - timedelta(days=7)).strftime("%Y-%m-%d")
    recent_words = [w for w in words if w.get("date", "") >= week_ago]
    if recent_words:
        vocab_lines = [
            f"**{w['term']}** — {w.get('zh', '')}\n> *{w.get('example_en', '')}*"
            for w in recent_words
        ]
        embeds.append(
            {
                "title": "📖 本週詞彙總複習",
                "description": "\n\n".join(vocab_lines)[:4096],
                "color": COLOR_WORD,
            }
        )
    return embeds


def post_discord_text(message: str) -> None:
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    chunk_size = 1900
    chunks = [message[i : i + chunk_size] for i in range(0, len(message), chunk_size)] or [message]
    for chunk in chunks:
        resp = requests.post(webhook_url, json={"content": chunk}, timeout=30)
        resp.raise_for_status()


def run_weekly(now_tw: datetime, today: str) -> None:
    week_files = sorted(ARCHIVE_DIR.glob("????-??-??.md"), reverse=True)[:7]
    texts = [p.read_text() for p in reversed(week_files)]
    if not texts:
        return
    weekly = call_gemini(build_weekly_prompt(texts, today))
    post_discord_embeds(build_weekly_embeds(weekly, load_words(), now_tw))


def main() -> None:
    prefs = load_prefs()
    fresh = gather_candidates()
    candidates = apply_prefs(fresh, prefs)
    now_tw = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    today = now_tw.strftime("%Y-%m-%d")

    if candidates:
        prompt = build_prompt(candidates, today, prefs, load_words())
        digest = call_gemini(prompt)
        stats = {
            "sources": len(RSS_SOURCES) + 6,  # +6: USGS、EMSC、台灣地震、JMA、NHC、JTWC
            "candidates": len(candidates),
            "picked": sum(len(digest.get(k, [])) for k in ("taiwan", "typhoon", "seismo", "other")),
        }
        post_discord_embeds(build_embeds(digest, now_tw, stats))
        write_archive(digest, now_tw)
        word = digest.get("word_of_the_day") or {}
        if word.get("term"):
            save_word(word, today)
    else:
        post_discord_text(f"**🌍 地球科學日報｜{today}**\n\n今日各來源皆無新內容，明天再見！")
    mark_seen(fresh)

    if now_tw.weekday() == 6:  # 週日加發回顧特刊
        try:
            run_weekly(now_tw, today)
        except Exception as exc:
            print(f"[warn] weekly review failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
