#!/usr/bin/env python3
"""Daily earthquake figure: record section (moveout) + official intensity map.

Picks the day's most significant event (Taiwan-area M4.5+ preferred, else
global M5.8+), fetches open waveforms from IRIS/EarthScope, plots a record
section with theoretical P/S travel-time curves, grabs the official USGS
ShakeMap intensity image when available, and posts both to Discord.
Skips quietly when there is no qualifying event.
"""
from __future__ import annotations

import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import requests
from obspy import UTCDateTime
from obspy.clients.fdsn import Client
from obspy.geodetics import locations2degrees
from obspy.taup import TauPyModel

LOOKBACK_HOURS = 30
TAIWAN_BBOX = {"minlatitude": 21, "maxlatitude": 26.5, "minlongitude": 118.5, "maxlongitude": 123.5}
TAIWAN_MIN_MAG = 4.5
GLOBAL_MIN_MAG = 5.8
USGS_QUERY_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"

MAX_TRACES = 12
SNR_MIN = 2.0


def pick_event() -> dict | None:
    start = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")
    for params in (
        {"minmagnitude": TAIWAN_MIN_MAG, **TAIWAN_BBOX},
        {"minmagnitude": GLOBAL_MIN_MAG},
    ):
        resp = requests.get(
            USGS_QUERY_URL,
            params={"format": "geojson", "starttime": start, "orderby": "magnitude", **params},
            timeout=30,
        )
        resp.raise_for_status()
        features = resp.json().get("features", [])
        if features:
            return features[0]
    return None


def fetch_shakemap_jpg(event_id: str) -> bytes | None:
    try:
        resp = requests.get(
            USGS_QUERY_URL,
            params={"format": "geojson", "eventid": event_id},
            timeout=30,
        )
        resp.raise_for_status()
        products = resp.json()["properties"].get("products", {})
        shakemap = products.get("shakemap")
        if not shakemap:
            return None
        url = shakemap[0]["contents"]["download/intensity.jpg"]["url"]
        img = requests.get(url, timeout=60)
        img.raise_for_status()
        return img.content
    except Exception as exc:
        print(f"[warn] shakemap fetch failed: {exc}", file=sys.stderr)
        return None


def choose_stations(client, ev_lat, ev_lon, origin, local: bool) -> list:
    if local:
        networks, min_deg, max_deg = "TW,IU,II,PS,JP", 0.2, 15.0
    else:
        networks, min_deg, max_deg = "IU,II", 5.0, 90.0
    inv = client.get_stations(
        network=networks, channel="BHZ", level="channel",
        starttime=origin, endtime=origin + 3600,
    )
    cands = []
    for net in inv:
        for sta in net:
            dist = locations2degrees(ev_lat, ev_lon, sta.latitude, sta.longitude)
            if min_deg <= dist <= max_deg:
                cands.append((dist, net.code, sta.code))
    cands.sort()
    return cands, min_deg, max_deg


def trace_snr(data: np.ndarray, sr: float, p_time: float) -> float:
    split = int(max(p_time - 20, 5) * sr)
    if split <= 0 or split >= len(data):
        return 0.0
    noise = np.std(data[:split]) or 1e-12
    signal = np.std(data[split:])
    return float(signal / noise)


def plot_section(event: dict) -> bytes | None:
    props = event["properties"]
    ev_lon, ev_lat, ev_depth = event["geometry"]["coordinates"]
    origin = UTCDateTime(props["time"] / 1000)
    local = props.get("place") and locations2degrees(ev_lat, ev_lon, 23.7, 121.0) < 5

    client = Client("IRIS")
    model = TauPyModel("iasp91")
    cands, min_deg, max_deg = choose_stations(client, ev_lat, ev_lon, origin, local)
    if len(cands) < 4:
        print("[warn] too few candidate stations, skipping section", file=sys.stderr)
        return None

    max_dist = max(d for d, _, _ in cands)
    arr = model.get_travel_times(source_depth_in_km=max(ev_depth, 0), distance_in_degree=max_dist, phase_list=["S"])
    duration = (arr[0].time + 240) if arr else 60 * 22

    fig, ax = plt.subplots(figsize=(9, 11))
    amp = max_dist / 40  # trace amplitude in y-axis units

    # 每個距離區間依序嘗試候選測站，取到一條乾淨波形就換下一區間
    span = (max_deg - min_deg) / MAX_TRACES
    bins = {}
    for dist, net, sta in cands:
        bins.setdefault(int((dist - min_deg) // span), []).append((dist, net, sta))

    n_ok, attempts = 0, 0
    for b in sorted(bins):
        for dist, net, sta in bins[b]:
            if attempts >= MAX_TRACES * 4:
                break
            attempts += 1
            st = None
            for loc in ("00", "", "10"):
                try:
                    st = client.get_waveforms(net, sta, loc, "BHZ", origin, origin + duration)
                    if st:
                        break
                except Exception:
                    continue
            if not st:
                continue
            tr = st.merge(fill_value=0)[0]
            if np.count_nonzero(tr.data) < len(tr.data) * 0.6:
                continue  # gappy/dead channel
            tr.detrend("demean")
            tr.filter("bandpass", freqmin=0.02, freqmax=1.0)
            data = tr.data.astype(float)

            p_arr = model.get_travel_times(source_depth_in_km=max(ev_depth, 0), distance_in_degree=dist, phase_list=["P", "Pn", "p"])
            p_time = p_arr[0].time if p_arr else duration / 4
            if trace_snr(data, tr.stats.sampling_rate, p_time) < SNR_MIN:
                continue  # too noisy to be educational

            peak = np.max(np.abs(data)) or 1.0
            t = np.arange(len(data)) * tr.stats.delta
            ax.plot(t, dist + data / peak * amp, lw=0.4, color="#333333")
            ax.text(duration * 1.01, dist, f"{net}.{sta}", fontsize=7, va="center")
            n_ok += 1
            break  # 這個距離區間完成

    if n_ok < 3:
        plt.close(fig)
        print("[warn] too few usable traces, skipping section", file=sys.stderr)
        return None

    dists = np.linspace(max(0.5, min_deg * 0.8), max_dist * 1.05, 60)
    for phases, color, label in ((["P", "Pn", "p"], "#D85A30", "P"), (["S", "Sn", "s"], "#378ADD", "S")):
        tt = []
        for dd in dists:
            a = model.get_travel_times(source_depth_in_km=max(ev_depth, 0), distance_in_degree=dd, phase_list=phases)
            tt.append(a[0].time if a else np.nan)
        ax.plot(tt, dists, color=color, lw=1.4, alpha=0.85, label=label)

    ax.set_xlim(0, duration)
    ax.set_ylim(0, max_dist * 1.12)
    ax.set_xlabel("Time since origin (s)")
    ax.set_ylabel("Epicentral distance (deg)")
    ax.set_title(
        f"Record section — {props['title']}\n"
        f"depth {ev_depth:.0f} km · vertical (BHZ), bandpass 0.02-1 Hz · IRIS/EarthScope open data"
    )
    ax.legend(loc="lower right")
    ax.grid(alpha=0.25)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return buf.getvalue()


SECTION_HOWTO = (
    "**怎麼看這張圖？**每條水平黑線是一個測站記錄到的垂直向地面振動，"
    "由下（離震央近）往上（遠）排列。🔴 紅線是理論 P 波走時、🔵 藍線是 S 波——"
    "測站越遠、波到得越晚，曲線的斜率就是 moveout，斜率倒數即視速度。"
    "P 波與 S 波的到時差越大代表距離越遠，這正是定位震央的原理。"
    "波形取自 IRIS/EarthScope 全球開放測站，走時曲線用 iasp91 地球模型計算。"
)


def post_figures(event: dict, section_png: bytes | None, shakemap_jpg: bytes | None) -> None:
    props = event["properties"]
    ev_lon, ev_lat, ev_depth = event["geometry"]["coordinates"]
    when = datetime.fromtimestamp(props["time"] / 1000, tz=timezone.utc).astimezone(
        timezone(timedelta(hours=8))
    ).strftime("%m/%d %H:%M 台灣時間")

    embeds, files = [], {}
    header = {
        "title": f"📈 今日焦點地震圖解：{props['title']}",
        "description": (
            f"規模 **{props['mag']}**｜深度 **{ev_depth:.0f} 公里**｜{when}\n"
            f"[USGS 事件頁 →]({props['url']})"
        ),
        "color": 0x2C3E50,
    }
    embeds.append(header)

    idx = 0
    if section_png:
        files[f"files[{idx}]"] = ("section.png", section_png, "image/png")
        embeds.append(
            {
                "title": "🌊 測站記錄剖面（moveout）",
                "description": SECTION_HOWTO,
                "color": 0x16A085,
                "image": {"url": "attachment://section.png"},
            }
        )
        idx += 1
    if shakemap_jpg:
        files[f"files[{idx}]"] = ("intensity.jpg", shakemap_jpg, "image/jpeg")
        embeds.append(
            {
                "title": "🗺️ USGS ShakeMap 震度圖",
                "description": "官方估計的地表震動強度分布（MMI 修訂麥卡利震度）。",
                "color": 0xC0392B,
                "image": {"url": "attachment://intensity.jpg"},
            }
        )

    payload = {
        "username": "地球科學日報",
        "avatar_url": "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/1f30d.png",
        "embeds": embeds,
    }

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        # 本地測試：存檔不發送
        out_dir = os.environ.get("QUAKEPLOT_OUT", ".")
        if section_png:
            open(os.path.join(out_dir, "section.png"), "wb").write(section_png)
        if shakemap_jpg:
            open(os.path.join(out_dir, "intensity.jpg"), "wb").write(shakemap_jpg)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    resp = requests.post(
        webhook_url,
        data={"payload_json": json.dumps(payload)},
        files=files or None,
        timeout=120,
    )
    resp.raise_for_status()


def main() -> None:
    event = pick_event()
    if not event:
        print("no qualifying earthquake in the past day; skipping figure")
        return
    print("target event:", event["properties"]["title"])
    section_png = plot_section(event)
    shakemap_jpg = fetch_shakemap_jpg(event["id"])
    if not section_png and not shakemap_jpg:
        print("no figure could be produced; skipping post")
        return
    post_figures(event, section_png, shakemap_jpg)


if __name__ == "__main__":
    main()
