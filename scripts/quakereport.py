#!/usr/bin/env python3
"""台灣地震報告：中央氣象署發布正式地震報告後，彙整成完整報告推播到 Discord 論壇。

用的是氣象署「顯著有感地震報告」(E-A0015-001) 與「小區域有感地震報告」
(E-A0016-001)——這是地震後數分鐘經人工審定的**正式報告**，含各地實測震度與
官方報告圖，不是自動產生的地震速報。

⚠️ 這不是地震預警系統。排程輪詢最快每 15 分鐘一次，且 GitHub Actions 的排程
本身可能再延遲數分鐘，訊息會在地震發生後約 15~40 分鐘才送達。要即時預警請用
氣象署的「警特報」App 或民生公共物聯網服務。

需要環境變數：CWA_API_KEY（氣象署開放資料授權碼）、DISCORD_WEBHOOK_QUAKE。
GOOGLE_API_KEY 為選配，有的話會加一段 AI 解說。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
SEEN_FILE = REPO_ROOT / "data" / "seen_quakereports.json"
SEEN_RETENTION_DAYS = 90

CWA_BASE = "https://opendata.cwa.gov.tw/api/v1/rest/datastore"
DATASETS = [
    ("E-A0015-001", "顯著有感地震報告"),
    ("E-A0016-001", "小區域有感地震報告"),
]
# 只推播達到這個規模或震度的報告，避免每天被小地震洗版（可自行調整）
MIN_MAGNITUDE = 4.0
MIN_INTENSITY_RANK = 2  # 震度 2 級

TW_TZ = timezone(timedelta(hours=8))

# 台灣震度分級（2020 年起細分 5 弱～6 強）
INTENSITY_RANK = {
    "0級": 0, "1級": 1, "2級": 2, "3級": 3, "4級": 4,
    "5弱": 5, "5強": 6, "6弱": 7, "6強": 8, "7級": 9,
}
INTENSITY_EMOJI = {
    0: "⚪", 1: "🟢", 2: "🟢", 3: "🟡", 4: "🟠",
    5: "🔴", 6: "🔴", 7: "🟣", 8: "🟣", 9: "⚫",
}
# 報告顏色 → embed 色碼（氣象署用綠/黃/紅標示規模等級）
REPORT_COLOR = {"綠色": 0x2ECC71, "黃色": 0xF1C40F, "紅色": 0xE74C3C}


def intensity_rank(text: str) -> int:
    """把「5弱」「3級」等字串轉成可比較的數值。"""
    if not text:
        return -1
    text = text.strip()
    if text in INTENSITY_RANK:
        return INTENSITY_RANK[text]
    for key, rank in INTENSITY_RANK.items():
        if key.rstrip("級") in text:
            return rank
    return -1


def load_seen() -> dict:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def save_seen(seen: dict) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SEEN_RETENTION_DAYS)).isoformat()
    pruned = {k: v for k, v in seen.items() if v > cutoff}
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(pruned, ensure_ascii=False, indent=2))


def fetch_reports() -> list:
    api_key = os.environ["CWA_API_KEY"]
    reports = []
    for dataset_id, dataset_name in DATASETS:
        try:
            resp = requests.get(
                f"{CWA_BASE}/{dataset_id}",
                params={"Authorization": api_key, "limit": 5, "format": "JSON"},
                timeout=30,
            )
            resp.raise_for_status()
            for quake in resp.json().get("records", {}).get("Earthquake", []):
                quake["_dataset"] = dataset_name
                reports.append(quake)
        except Exception as exc:
            print(f"[warn] {dataset_id} fetch failed: {exc}", file=sys.stderr)
    return reports


def parse_report(quake: dict) -> dict:
    """把氣象署的巢狀結構整理成好用的欄位。所有數值原封不動照抄，不做任何推算。"""
    info = quake.get("EarthquakeInfo", {})
    epicenter = info.get("Epicenter", {})
    magnitude = info.get("EarthquakeMagnitude", {})

    areas = []
    for area in (quake.get("Intensity", {}) or {}).get("ShakingArea", []):
        # 氣象署同時提供縣市層級與細部地區，只取縣市層級（有 CountyName）
        county = area.get("CountyName")
        if not county:
            continue
        stations = [
            {
                "name": s.get("StationName", ""),
                "intensity": s.get("SeismicIntensity", ""),
                "rank": intensity_rank(s.get("SeismicIntensity", "")),
            }
            for s in area.get("EqStation", []) or []
        ]
        stations.sort(key=lambda s: s["rank"], reverse=True)
        areas.append(
            {
                "county": county,
                "intensity": area.get("AreaIntensity", ""),
                "rank": intensity_rank(area.get("AreaIntensity", "")),
                "stations": stations,
            }
        )
    areas.sort(key=lambda a: a["rank"], reverse=True)

    origin_raw = info.get("OriginTime", "")
    try:
        origin = datetime.strptime(origin_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TW_TZ)
    except ValueError:
        origin = None

    return {
        "no": str(quake.get("EarthquakeNo", "")),
        "dataset": quake.get("_dataset", ""),
        "report_type": quake.get("ReportType", "地震報告"),
        "color": quake.get("ReportColor", ""),
        "content": quake.get("ReportContent", ""),
        "remark": quake.get("ReportRemark", ""),
        "image": quake.get("ReportImageURI", ""),
        "shakemap": quake.get("ShakemapImageURI", ""),
        "web": quake.get("Web", ""),
        "origin_raw": origin_raw,
        "origin": origin,
        "depth": info.get("FocalDepth"),
        "location": epicenter.get("Location", ""),
        "lat": epicenter.get("EpicenterLatitude"),
        "lon": epicenter.get("EpicenterLongitude"),
        "mag_type": magnitude.get("MagnitudeType", "芮氏規模"),
        "mag": magnitude.get("MagnitudeValue"),
        "source": info.get("Source", "中央氣象署"),
        "areas": areas,
    }


def is_noteworthy(r: dict) -> bool:
    mag = r["mag"] or 0
    top_rank = r["areas"][0]["rank"] if r["areas"] else -1
    return mag >= MIN_MAGNITUDE or top_rank >= MIN_INTENSITY_RANK


AI_PROMPT = """你是一位台灣的地震學科普作者。以下是中央氣象署剛發布的正式地震報告，請寫一段 200~300 字的中文解說，幫助讀者理解這起地震。

要求：
- 用繁體中文與台灣學術用語（例：地函非地幔、規模非震級、隱沒非俯衝）。
- 解說這起地震的地體構造背景：發生在哪個構造帶（如菲律賓海板塊與歐亞板塊的聚合帶、琉球隱沒帶、花東縱谷斷層帶、西部麓山帶等），為什麼這個位置會有地震。
- 說明這個深度與規模的意義（如淺層地震為何搖得比較劇烈、這個規模大約多久發生一次）。
- 若資訊中有海嘯相關說明就轉述；沒有的話不要自行臆測海嘯風險。
- 只能根據以下提供的事實撰寫，不可自行加入報告中沒有的數字或災情描述。不確定的構造背景就寫得保守一些。
- 語氣像泛科學的文章，專業但好讀。結尾可留一個知識點。
- 絕對不要給防災指示或安全建議（那是氣象署與消防單位的職責）。

地震事實：
- 發震時間：{origin}
- 震央：{location}（北緯 {lat} 度、東經 {lon} 度）
- 深度：{depth} 公里
- 規模：{mag_type} {mag}
- 各地最大震度：{intensity_summary}
- 氣象署報告內容：{content}
{remark_line}

請只輸出解說文字本身，不要加標題或前言。"""


def ai_explanation(r: dict) -> str:
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return ""
    intensity_summary = "、".join(f"{a['county']} {a['intensity']}" for a in r["areas"][:6]) or "（無資料）"
    prompt = AI_PROMPT.format(
        origin=r["origin_raw"], location=r["location"], lat=r["lat"], lon=r["lon"],
        depth=r["depth"], mag_type=r["mag_type"], mag=r["mag"],
        intensity_summary=intensity_summary, content=r["content"],
        remark_line=f"- 附註：{r['remark']}" if r["remark"] else "",
    )
    for model in ("gemini-flash-latest", "gemini-flash-lite-latest"):
        try:
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": api_key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2000},
                },
                timeout=90,
            )
            resp.raise_for_status()
            parts = resp.json()["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts).strip()
        except Exception as exc:
            print(f"[warn] AI explanation via {model} failed: {exc}", file=sys.stderr)
    return ""


def build_embeds(r: dict, explanation: str) -> list:
    top = r["areas"][0] if r["areas"] else None
    emoji = INTENSITY_EMOJI.get(top["rank"], "📍") if top else "📍"
    color = REPORT_COLOR.get(r["color"], 0x34495E)

    lines = [
        f"**{r['report_type']}**　編號 {r['no']}",
        "",
        f"🕐 **發震時間**　{r['origin_raw']}（台灣時間）",
        f"📍 **震央位置**　{r['location']}",
        f"🌐 **經緯度**　　北緯 {r['lat']} 度、東經 {r['lon']} 度",
        f"⬇️ **焦點深度**　{r['depth']} 公里",
        f"📊 **規模**　　　{r['mag_type']} {r['mag']}",
    ]
    if top:
        lines.append(f"{emoji} **最大震度**　{top['county']} {top['intensity']}")
    if r["content"]:
        lines += ["", f"> {r['content']}"]
    if r["remark"]:
        lines.append(f"> {r['remark']}")
    lines += ["", f"-# 資料來源：{r['source']}｜{r['dataset']}"]
    if r["web"]:
        lines.append(f"-# [氣象署原始報告 →]({r['web']})")

    summary = {
        "title": f"{emoji} 地震報告：{r['location'].split('（')[0].strip()}　規模 {r['mag']}",
        "description": "\n".join(lines)[:4096],
        "color": color,
    }

    embeds = [summary]

    # 各地震度：依震度由大到小，附該縣市震度最大的測站
    if r["areas"]:
        rows = []
        for a in r["areas"]:
            e = INTENSITY_EMOJI.get(a["rank"], "▪️")
            row = f"{e} **{a['county']}**　{a['intensity']}"
            if a["stations"]:
                tops = [s for s in a["stations"] if s["rank"] == a["rank"]][:3]
                names = "、".join(s["name"] for s in tops)
                if names:
                    row += f"\n-# 　最大測站：{names}（共 {len(a['stations'])} 站有紀錄）"
            rows.append(row)
        chunks, current = [], ""
        for row in rows:
            candidate = f"{current}\n{row}" if current else row
            if len(candidate) > 3800 and current:
                chunks.append(current)
                current = row
            else:
                current = candidate
        if current:
            chunks.append(current)
        for i, chunk in enumerate(chunks):
            embeds.append(
                {
                    "title": "📈 各地最大震度" + ("（續）" if i else ""),
                    "description": chunk[:4096],
                    "color": color,
                }
            )

    if r["image"]:
        embeds.append(
            {
                "title": "🗺️ 氣象署地震報告圖",
                "description": "官方發布的震央位置與各地震度分布圖。",
                "color": color,
                "image": {"url": r["image"]},
            }
        )
    if r["shakemap"] and r["shakemap"] != r["image"]:
        embeds.append(
            {
                "title": "🌈 等震圖（ShakeMap）",
                "color": color,
                "image": {"url": r["shakemap"]},
            }
        )

    if explanation:
        embeds.append(
            {
                "title": "🔬 這起地震在說什麼？",
                "description": explanation[:4096],
                "color": 0x9B59B6,
                "footer": {"text": "AI 依據氣象署報告內容撰寫的科普解說，非官方判釋"},
            }
        )

    return embeds


def embed_size(e: dict) -> int:
    return (
        len(e.get("title", ""))
        + len(e.get("description", ""))
        + len(e.get("footer", {}).get("text", ""))
    )


def post(r: dict, embeds: list) -> None:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_QUAKE", "")
    top = r["areas"][0] if r["areas"] else None
    when = r["origin"].strftime("%m/%d %H:%M") if r["origin"] else r["origin_raw"][:16]
    place = r["location"].split("（")[0].strip()
    thread_name = f"🚨 M{r['mag']} {place}｜{when}"
    if top:
        thread_name += f"｜最大震度 {top['county']}{top['intensity']}"

    if not webhook_url:
        print("=== DRY RUN:", thread_name, "===")
        print(json.dumps(embeds, ensure_ascii=False, indent=2)[:3000])
        return

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

    thread_id = None
    for b in batches:
        body = {"username": "台灣地震報告", "embeds": b}
        params = {"wait": "true"}
        if thread_id:
            params["thread_id"] = thread_id
        else:
            body["thread_name"] = thread_name[:100]
        resp = requests.post(webhook_url, params=params, json=body, timeout=30)
        if resp.status_code == 400 and not thread_id:
            body.pop("thread_name", None)
            resp = requests.post(webhook_url, params={"wait": "true"}, json=body, timeout=30)
        resp.raise_for_status()
        if thread_id is None:
            thread_id = resp.json().get("channel_id")


def main() -> None:
    seen = load_seen()
    reports = [parse_report(q) for q in fetch_reports()]
    now = datetime.now(timezone.utc).isoformat()

    new = [r for r in reports if r["no"] and r["no"] not in seen]
    if not new:
        print("no new earthquake reports")
        return

    new.sort(key=lambda r: r["origin_raw"])
    for r in new:
        if not is_noteworthy(r):
            print(f"skip (below threshold): {r['no']} M{r['mag']}")
            seen[r["no"]] = now
            continue
        print(f"posting report {r['no']}: M{r['mag']} {r['location']}")
        try:
            post(r, build_embeds(r, ai_explanation(r)))
            seen[r["no"]] = now
        except Exception as exc:
            print(f"[error] failed to post {r['no']}: {exc}", file=sys.stderr)
    save_seen(seen)


if __name__ == "__main__":
    main()
