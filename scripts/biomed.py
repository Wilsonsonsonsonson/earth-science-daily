#!/usr/bin/env python3
"""醫學與生物日報：與地球科學日報共用 digest.py 引擎，換上生醫來源、版面與提示詞。

執行前需設定 DISCORD_WEBHOOK_BIOMED（生醫論壇的 Webhook）與 GOOGLE_API_KEY。
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import digest as d

REPO_ROOT = d.REPO_ROOT

# ── 刊物設定 ─────────────────────────────────────────────
d.WEBHOOK_ENV = "DISCORD_WEBHOOK_BIOMED"
d.BOT_NAME = "醫學生物日報"
d.AVATAR_URL = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/1f9ec.png"
d.HEADER_TITLE = "🧬 醫學生物日報 · Life Science Daily"
d.THREAD_PREFIX = "🧬 醫學生物日報"
d.ARCHIVE_TITLE = "📚 醫學生物日報歸檔"
d.READER_FIELD = "醫學與生命科學"
d.EDITION_ROLE = "一份醫學與生物日報的主編"
d.WORD_DOMAIN = "醫學與生命科學（分子生物、免疫、神經科學、流行病學、臨床醫學）"
d.WORD_EXAMPLES = "in vitro、cohort、pathogenesis、efficacy"
d.DIVE_HINT = "優先挑機制研究或對人類健康影響大的題目"
d.EMOJI_HINT = "🧬 遺傳、🦠 微生物、💊 藥物、🧠 神經、🫀 生理、🔬 方法、📊 流行病學、🧫 細胞"
d.PREPRINT_LABEL = "bioRxiv／medRxiv 預印本（未經同儕審查）"
d.EXTRA_SOURCE_COUNT = 0  # 生醫版全部來自 RSS

# 專屬的資料檔與歸檔目錄（與地科版分開，不互相干擾）
d.DATA_FILE = REPO_ROOT / "data" / "seen_biomed.json"
d.WORDS_FILE = REPO_ROOT / "data" / "words_biomed.json"
d.THREAD_FILE = REPO_ROOT / "data" / "last_thread_biomed.json"
d.ARCHIVE_DIR = REPO_ROOT / "archive-biomed"
d.PREFS_FILE = REPO_ROOT / "preferences-biomed.json"

# ── 來源白名單 ───────────────────────────────────────────
# 只收錄頂尖期刊與權威機構。預印本（bioRxiv/medRxiv）標記 peer_reviewed=False，
# 日報中會明確標註未經同儕審查——生醫領域的預印本尤其需要謹慎解讀。
d.RSS_SOURCES = [
    # 臨床醫學（clinical）
    {"name": "The Lancet", "url": "https://www.thelancet.com/rssfeed/lancet_current.xml", "category": "clinical"},
    {"name": "New England Journal of Medicine", "url": "https://www.nejm.org/action/showFeed?type=etoc&feed=rss&jc=nejm", "category": "clinical"},
    {"name": "Nature Medicine", "url": "https://www.nature.com/nm.rss", "category": "clinical"},
    {"name": "PLOS Medicine", "url": "https://journals.plos.org/plosmedicine/feed/atom", "category": "clinical"},
    {"name": "medRxiv 臨床預印本", "url": "https://connect.medrxiv.org/medrxiv_xml.php?subject=all", "category": "clinical", "peer_reviewed": False},
    # 生命科學（life）
    {"name": "Cell", "url": "https://www.cell.com/cell/current.rss", "category": "life"},
    {"name": "Nature", "url": "https://www.nature.com/nature.rss", "category": "life"},
    {"name": "PLOS Biology", "url": "https://journals.plos.org/plosbiology/feed/atom", "category": "life"},
    {"name": "bioRxiv 生物預印本", "url": "https://connect.biorxiv.org/biorxiv_xml.php?subject=all", "category": "life", "peer_reviewed": False},
    # 公共衛生與科學新聞（health）
    {"name": "WHO 世界衛生組織新聞", "url": "https://www.who.int/rss-feeds/news-english.xml", "category": "health"},
    {"name": "Science 新聞", "url": "https://www.science.org/rss/news_current.xml", "category": "health"},
]

# ── 版面 ─────────────────────────────────────────────────
COLOR_CLINICAL = 0xE74C3C   # 紅：臨床醫學
COLOR_LIFE = 0x27AE60       # 綠：生命科學
COLOR_HEALTH = 0x3498DB     # 藍：公共衛生

d.SECTION_DEFS = [
    ("clinical", "🩺 臨床醫學與人體健康", COLOR_CLINICAL, "今日無重大更新。"),
    ("life", "🧬 生命科學與分子生物", COLOR_LIFE, "今日無重大更新。"),
    ("health", "🌐 公共衛生與全球疫情", COLOR_HEALTH, "今日無重大更新。"),
]

d.CATEGORY_RULES = """- [clinical]：臨床醫學與人體健康（臨床試驗、治療、診斷、藥物、疾病機制）。這是本日報的主要焦點，最多挑 6 則。
- [life]：生命科學與分子生物（基因、細胞、免疫、神經科學、演化、微生物）。最多挑 5 則。
- [health]：公共衛生與全球疫情（WHO 公告、疫情、流行病學、健康政策）。最多挑 3 則，不必每天都有。"""

d.EXTRA_RULES = """- 醫學資訊攸關人命，正確性優先於趣味性。以下規則必須嚴格遵守：
  - 絕對不可將研究結果寫成醫療建議，不可暗示讀者該吃什麼藥、做什麼治療。必要時寫「這是研究發現，不是治療指引」。
  - 動物實驗、細胞實驗的結果，一定要明講研究對象（如「在小鼠身上」「在細胞培養中」），絕不可寫成人體已證實。
  - 臨床試驗要交代期別（第一／二／三期）與樣本數；觀察性研究要點出「相關不等於因果」。
  - 預印本（bioRxiv／medRxiv）未經同儕審查，入選標準從嚴，且務必標註，寫法要更保守。
  - 療效數字照抄原文，不可換算或誇大；不確定的細節寧可不寫。
- 這份日報是給學習者看的科學新聞，不是給病人看的衛教——重點放在「科學上發現了什麼、方法為何巧妙、還有什麼限制」。"""

d.SCHEMA_SECTIONS = ",\n".join(
    f'  "{key}": [\n    {d._ITEM_SCHEMA.format(emoji=emoji)}\n  ]'
    for key, emoji in (("clinical", "🩺"), ("life", "🧬"), ("health", "🌐"))
)


def gather_candidates() -> list:
    """生醫版只有 RSS 來源，沒有地震／颱風那類即時 API。"""
    now = datetime.now(timezone.utc)
    seen = d.load_seen()
    candidates = []
    for source in d.RSS_SOURCES:
        candidates.extend(d.fetch_rss(source, now))

    fresh, keys = [], set()
    for c in candidates:
        key = c.get("dedupe_key") or c["link"]
        if key and key not in seen and key not in keys:
            fresh.append(c)
            keys.add(key)
    return fresh


d.gather_candidates = gather_candidates

if __name__ == "__main__":
    d.main()
