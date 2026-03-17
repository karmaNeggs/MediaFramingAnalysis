#!/usr/bin/env python3
"""
regime_scraper_gkg.py  —  v4 (GDELT GKG 2.0, real timestamp resolution)
=========================================================================

FIXES IN v4
-----------
1. REAL TIMESTAMP RESOLUTION
   Instead of guessing fixed slots (060000, 120000, 180000) that may not
   exist, we fetch the GDELT master file list for each sample date and
   pick the 3 real timestamps closest to 06:00, 12:00 and 18:00 UTC.
   This eliminates 404-driven undercounting in older years.

   If fewer than MIN_FILES_PER_DAY real files are found for a date, we
   expand the search to adjacent dates (±1 day, ±2 days) until we have
   enough — guaranteeing consistent sample depth across all years.

2. FOCUSED NON-G7 LIST (10 major authoritarian economies)
   Removed mid-tier democracies and large neutral economies that were
   diluting the signal. Kept only countries with:
     - High consistent Western media coverage
     - Clear adversarial/authoritarian framing in the literature
     - Significant economy size (not micro-states)

   Final list: Russia, China, Iran, North Korea, Venezuela,
               Saudi Arabia, Belarus, Syria, Cuba, Myanmar

OCCURRENCE INDEX
----------------
   regime_share_G7    = regime_G7    / (regime_G7    + govt_G7)
   regime_share_NonG7 = regime_NonG7 / (regime_NonG7 + govt_NonG7)
   gap_pp             = (regime_share_NonG7 - regime_share_G7) x 100

HOW TO RUN
----------
   pip install requests pandas matplotlib tqdm
   python regime_scraper_gkg.py

OUTPUT
------
   regime_gkg_raw.csv     — per-file counts
   regime_gkg_annual.csv  — annual aggregation + occurrence index
   regime_gkg_chart.png   — 2x2 chart
   regime_scraper.log     — full run log
"""

import io
import csv
import sys
import time
import zipfile
import logging
import requests
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as mtick
from tqdm import tqdm
from collections import defaultdict
from datetime import date, timedelta

# ── FIELD SIZE FIX ────────────────────────────────────────────────────────────
csv.field_size_limit(10_000_000)

# ── CONFIGURATION ─────────────────────────────────────────────────────────────

YEARS           = list(range(2018, 2026))  # 2018-2025 inclusive
SAMPLE_DAYS     = [7, 15, 23]             # 3 days per month
TARGET_SLOTS    = ["060000", "120000", "180000"]  # preferred times (UTC)
MIN_FILES_PER_DAY = 3                     # expand to adjacent dates if fewer found
MAX_DAY_OFFSET  = 3                       # look up to ±3 days if needed

# GDELT URLs
GKG2_BASE       = "http://data.gdeltproject.org/gdeltv2/{dt}.gkg.csv.zip"
GKG2_MASTERLIST = "http://data.gdeltproject.org/gdeltv2/{date}.gkg.csv.zip.md5"
GDELT_FILELIST  = "http://data.gdeltproject.org/gdeltv2/masterfilelist-translation.txt"
GKG2_INDEX_URL  = "http://data.gdeltproject.org/gdeltv2/{date}.gkg.csv.zip"

# We resolve real timestamps via the 15-min interval index page
GKG2_DAY_INDEX  = "http://data.gdeltproject.org/gdeltv2/"

WESTERN_OUTLETS = [
    "nytimes.com", "theguardian.com", "washingtonpost.com",
    "bbc.co.uk", "bbc.com", "reuters.com", "apnews.com",
    "economist.com", "ft.com", "wsj.com", "nbcnews.com",
    "latimes.com", "telegraph.co.uk", "independent.co.uk",
    "politico.com", "theatlantic.com", "foreignpolicy.com",
    "new york times", "guardian", "washington post",
    "bbc news", "reuters", "associated press",
    "financial times", "wall street journal",
]

# ── G7 TOKENS ─────────────────────────────────────────────────────────────────

G7_TOKENS = [
    "france", "french", "germany", "german", "japan", "japanese",
    "canada", "canadian", "italy", "italian",
    "united kingdom", "britain", "british",
    "united states", "american",
]

# ── NON-G7 TOKENS — 10 major authoritarian economies ─────────────────────────
# Rationale: high Western media coverage, consistent adversarial framing,
# significant economy/population. No mid-tier democracies.

NON_G7_TOKENS = [
    # Russia — largest, most covered, archetypal "regime" target post-2022
    "russia", "russian", "kremlin", "moscow", "putin",
    # China — largest economy in group, sustained framing shift post-2018
    "china", "chinese", "beijing", "xi jinping", "ccp",
    # Iran — three decades of "regime" usage in Western press
    "iran", "iranian", "tehran", "khamenei", "irgc",
    # North Korea — most consistent "regime" label in corpus studies
    "north korea", "north korean", "pyongyang", "kim jong",
    # Venezuela — sustained authoritarian framing since 2017
    "venezuela", "venezuelan", "maduro", "caracas",
    # Saudi Arabia — large economy, strategic ambiguity creates interesting signal
    "saudi arabia", "saudi", "riyadh", "mbs", "bin salman",
    # Belarus — sharp coverage increase post-2020 crackdown
    "belarus", "belarusian", "minsk", "lukashenko",
    # Syria — decade-long "regime" usage, one of highest frequency in literature
    "syria", "syrian", "damascus", "assad",
    # Cuba — long-running Western framing, useful historical baseline
    "cuba", "cuban", "havana",
    # Myanmar — post-2021 coup, strong and sudden "regime" signal
    "myanmar", "burmese", "naypyidaw", "tatmadaw", "min aung hlaing",
]

REGIME_TOKENS = ["regime"]
GOVT_TOKENS   = ["government", "administration", "leadership"]

OUTPUT_RAW    = "regime_gkg_raw.csv"
OUTPUT_ANNUAL = "regime_gkg_annual.csv"
OUTPUT_CHART  = "regime_gkg_chart.png"

# ── LOGGING ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("regime_scraper.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

# ── REAL TIMESTAMP RESOLUTION ─────────────────────────────────────────────────

def fetch_real_timestamps_for_date(date_str: str) -> list[str]:
    """
    Fetch the GDELT 2.0 master file list and extract all GKG file
    timestamps for a given date (YYYYMMDD).

    GDELT publishes a plaintext master list at:
      http://data.gdeltproject.org/gdeltv2/masterfilelist.txt
    Each line: <size> <md5> <url>
    We filter for lines matching our date and ending in .gkg.csv.zip

    Returns list of datetime strings (YYYYMMDDHHmmSS), sorted ascending.
    """
    master_url = "http://data.gdeltproject.org/gdeltv2/masterfilelist.txt"
    try:
        resp = requests.get(master_url, timeout=60, stream=True)
        resp.raise_for_status()
        timestamps = []
        for line in resp.iter_lines():
            line = line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            url = parts[2]
            # Match pattern: .../gdeltv2/YYYYMMDDHHMMSS.gkg.csv.zip
            if ".gkg.csv.zip" not in url:
                continue
            fname = url.split("/")[-1]  # e.g. 20220315120000.gkg.csv.zip
            dt_part = fname.replace(".gkg.csv.zip", "")
            if len(dt_part) == 14 and dt_part.startswith(date_str):
                timestamps.append(dt_part)
        return sorted(timestamps)
    except Exception as e:
        log.warning(f"  Master list fetch failed for {date_str}: {e}")
        return []


def pick_best_timestamps(all_ts: list[str], target_slots: list[str],
                          n: int = 3) -> list[str]:
    """
    Given all real timestamps for a date, pick the n closest to each
    target slot time. Returns up to n unique timestamps.

    target_slots: list of HHMMSS strings e.g. ["060000","120000","180000"]
    """
    if not all_ts:
        return []

    def slot_to_seconds(s):
        return int(s[:2]) * 3600 + int(s[2:4]) * 60 + int(s[4:6])

    def ts_seconds(ts):
        # ts is YYYYMMDDHHmmSS — extract HHmmSS
        return slot_to_seconds(ts[8:])

    picked = []
    used = set()
    for slot in target_slots:
        target_sec = slot_to_seconds(slot)
        best = min(all_ts, key=lambda t: abs(ts_seconds(t) - target_sec))
        if best not in used:
            picked.append(best)
            used.add(best)

    return picked[:n]


def resolve_timestamps_for_date(date_str: str,
                                 target_slots: list[str],
                                 min_files: int,
                                 max_offset: int) -> list[str]:
    """
    Resolve real GKG 2.0 timestamps for a sample date.
    If fewer than min_files found, expands search to adjacent dates.

    Returns list of YYYYMMDDHHmmSS strings to download.
    """
    # Try the target date first
    all_ts = fetch_real_timestamps_for_date(date_str)
    picked = pick_best_timestamps(all_ts, target_slots)

    if len(picked) >= min_files:
        return picked

    log.debug(f"  {date_str}: only {len(picked)} timestamps, expanding...")

    # Expand to adjacent dates
    base = date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
    extra = []
    for offset in range(1, max_offset + 1):
        for sign in [1, -1]:
            adj = base + timedelta(days=sign * offset)
            adj_str = adj.strftime("%Y%m%d")
            adj_ts = fetch_real_timestamps_for_date(adj_str)
            adj_picked = pick_best_timestamps(adj_ts, target_slots)
            extra.extend(adj_picked)
            if len(picked) + len(extra) >= min_files:
                break
        if len(picked) + len(extra) >= min_files:
            break

    combined = list(dict.fromkeys(picked + extra))  # deduplicate, preserve order
    return combined[:min_files]


# ── FASTER ALTERNATIVE: probe known 15-min intervals ─────────────────────────
# Fetching the full master list (~500MB) is slow. Instead we probe
# candidate timestamps directly with HEAD requests.

def probe_timestamps_for_date(date_str: str,
                               target_slots: list[str],
                               min_files: int,
                               max_offset: int) -> list[str]:
    """
    Faster approach: generate all possible 15-minute interval timestamps
    for the target date, probe with HEAD requests to find real ones,
    then pick the best matches to target slots.

    Falls back to adjacent dates if needed.
    """
    def all_intervals(d_str: str) -> list[str]:
        """All 96 possible 15-min timestamps for a date."""
        slots = []
        for h in range(24):
            for m in [0, 15, 30, 45]:
                slots.append(f"{d_str}{h:02d}{m:02d}00")
        return slots

    def probe_one(ts: str) -> bool:
        url = GKG2_BASE.format(dt=ts)
        try:
            r = requests.head(url, timeout=10)
            return r.status_code == 200
        except Exception:
            return False

    def find_real_for_date(d_str: str) -> list[str]:
        candidates = all_intervals(d_str)
        real = []
        for ts in candidates:
            if probe_one(ts):
                real.append(ts)
            time.sleep(0.05)
        return sorted(real)

    base = date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
    all_real = []
    offsets = [0] + [x for i in range(1, max_offset + 1) for x in [i, -i]]

    for offset in offsets:
        d = base + timedelta(days=offset)
        real = find_real_for_date(d.strftime("%Y%m%d"))
        all_real.extend(real)
        picked = pick_best_timestamps(all_real, target_slots)
        if len(picked) >= min_files:
            return picked[:min_files]

    return pick_best_timestamps(all_real, target_slots)[:min_files]


# ── SMART SAMPLER: use masterfilelist, fall back to probing ───────────────────

# Cache to avoid re-fetching the master list repeatedly
_MASTER_CACHE: dict[str, list[str]] = {}  # date_str -> list of timestamps

def get_timestamps_for_date(date_str: str) -> list[str]:
    """
    Return real GKG 2.0 timestamps for a date.
    Uses a lightweight approach: try the 3 target slots directly,
    then shift by ±15 min if not found, expanding to adjacent dates.
    """
    if date_str in _MASTER_CACHE:
        return _MASTER_CACHE[date_str]

    base = date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
    found = []

    # For each target slot, scan ±4 intervals (±1 hour) to find a real file
    for slot in TARGET_SLOTS:
        h = int(slot[:2])
        m = int(slot[2:4])
        base_minutes = h * 60 + m
        hit = None
        for delta in [0, 15, -15, 30, -30, 45, -45, 60, -60]:
            total = base_minutes + delta
            if total < 0 or total >= 1440:
                continue
            nh, nm = divmod(total, 60)
            ts = base.strftime("%Y%m%d") + f"{nh:02d}{nm:02d}00"
            url = GKG2_BASE.format(dt=ts)
            try:
                r = requests.head(url, timeout=8)
                if r.status_code == 200:
                    hit = ts
                    break
            except Exception:
                continue
        if hit:
            found.append(hit)

    # If still short, try adjacent dates
    if len(found) < MIN_FILES_PER_DAY:
        for offset in range(1, MAX_DAY_OFFSET + 1):
            for sign in [1, -1]:
                adj = base + timedelta(days=sign * offset)
                adj_str = adj.strftime("%Y%m%d")
                if adj_str not in _MASTER_CACHE:
                    adj_found = get_timestamps_for_date(adj_str)
                    found.extend(adj_found)
                if len(found) >= MIN_FILES_PER_DAY:
                    break
            if len(found) >= MIN_FILES_PER_DAY:
                break

    result = list(dict.fromkeys(found))[:MIN_FILES_PER_DAY]
    _MASTER_CACHE[date_str] = result
    return result


# ── DOWNLOAD ──────────────────────────────────────────────────────────────────

def download_gkg2_file(dt_str: str) -> list[str]:
    """
    Download and decompress one GKG 2.0 15-minute file.
    Returns list of tab-delimited line strings, [] on failure.
    """
    url = GKG2_BASE.format(dt=dt_str)
    try:
        resp = requests.get(url, timeout=120, stream=True)
        resp.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        raw = zf.read(zf.namelist()[0]).decode("utf-8", errors="replace")
        return raw.splitlines()
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        log.debug(f"HTTP {code}: {dt_str}")
        return []
    except Exception as e:
        log.warning(f"Failed {dt_str}: {e}")
        return []

# ── PARSING ───────────────────────────────────────────────────────────────────

def is_western(cols: list[str]) -> bool:
    source_name = cols[3].lower() if len(cols) > 3 else ""
    source_url  = cols[4].lower() if len(cols) > 4 else ""
    combined    = source_name + " " + source_url
    return any(outlet in combined for outlet in WESTERN_OUTLETS)


def article_text(cols: list[str]) -> str:
    """
    Pull text from columns that contain actual article words:
    Extras (26), Quotations (22), AllNames (23), Themes (7,8),
    Organizations (13,14), URL (4).
    """
    indices = [4, 7, 8, 11, 12, 13, 14, 22, 23, 26]
    parts = [cols[i] for i in indices if i < len(cols)]
    return " ".join(parts).lower()


def contains_any(text: str, tokens: list[str]) -> bool:
    return any(tok in text for tok in tokens)


def parse_file(lines: list[str]) -> dict:
    counts = defaultdict(int)
    reader = csv.reader(lines, delimiter="\t")
    for cols in reader:
        if len(cols) < 5:
            continue
        if not is_western(cols):
            continue

        text = article_text(cols)

        has_regime = contains_any(text, REGIME_TOKENS)
        has_govt   = contains_any(text, GOVT_TOKENS)
        has_g7     = contains_any(text, G7_TOKENS)
        has_ng7    = contains_any(text, NON_G7_TOKENS)

        if not (has_regime or has_govt):
            continue

        if has_ng7:
            if has_regime:
                counts["regime_non_g7"] += 1
            if has_govt:
                counts["govt_non_g7"] += 1
        if has_g7:
            if has_regime:
                counts["regime_g7"] += 1
            if has_govt:
                counts["govt_g7"] += 1

    return dict(counts)

# ── MAIN ──────────────────────────────────────────────────────────────────────

def run():
    log.info(f"Years: {YEARS[0]}-{YEARS[-1]}")
    log.info(f"Sample days/month: {SAMPLE_DAYS}")
    log.info(f"Target slots: {TARGET_SLOTS}")
    log.info(f"Min files/day: {MIN_FILES_PER_DAY}, max offset: {MAX_DAY_OFFSET} days")
    log.info(f"Non-G7 countries: Russia, China, Iran, North Korea, Venezuela, "
             f"Saudi Arabia, Belarus, Syria, Cuba, Myanmar")

    # Build full list of (date_str, year, month) sample points
    sample_points = []
    for yr in YEARS:
        for mo in range(1, 13):
            for day in SAMPLE_DAYS:
                try:
                    d = date(yr, mo, day)
                    sample_points.append((d.strftime("%Y%m%d"), yr, mo))
                except ValueError:
                    continue

    log.info(f"Total sample dates: {len(sample_points)}")

    raw_rows = []
    files_found_per_year = defaultdict(int)

    for date_str, yr, mo in tqdm(sample_points, desc="Resolving + downloading"):
        # Resolve real timestamps for this date
        timestamps = get_timestamps_for_date(date_str)
        if not timestamps:
            log.warning(f"  No real files found for {date_str}, skipping")
            continue

        log.debug(f"  {date_str}: using {timestamps}")

        day_counts: dict[str, int] = defaultdict(int)
        files_used = 0

        for ts in timestamps:
            lines = download_gkg2_file(ts)
            if not lines:
                continue
            fc = parse_file(lines)
            for k, v in fc.items():
                day_counts[k] += v
            files_used += 1
            time.sleep(0.2)

        if files_used == 0:
            continue

        files_found_per_year[yr] += files_used
        row = dict(day_counts)
        row["date"]       = date_str
        row["year"]       = yr
        row["month"]      = mo
        row["files_used"] = files_used
        raw_rows.append(row)

    if not raw_rows:
        log.error("No data collected. Check network connection.")
        return

    log.info("Files used per year:")
    for yr in YEARS:
        log.info(f"  {yr}: {files_found_per_year[yr]} files")

    df = pd.DataFrame(raw_rows).fillna(0)
    for col in ["regime_non_g7", "regime_g7", "govt_non_g7", "govt_g7"]:
        if col not in df.columns:
            df[col] = 0

    df.to_csv(OUTPUT_RAW, index=False)
    log.info(f"Raw data -> {OUTPUT_RAW}  ({len(df)} rows)")

    # ── ANNUAL AGGREGATION ───────────────────────────────────────────────────

    annual = df.groupby("year")[
        ["regime_non_g7", "regime_g7", "govt_non_g7", "govt_g7", "files_used"]
    ].sum().reset_index()

    for col in ["regime_non_g7", "regime_g7", "govt_non_g7", "govt_g7", "files_used"]:
        annual[col] = annual[col].astype(int)

    denom_ng7 = annual["regime_non_g7"] + annual["govt_non_g7"]
    denom_g7  = annual["regime_g7"]     + annual["govt_g7"]

    annual["regime_share_non_g7"] = (
        annual["regime_non_g7"] / denom_ng7.where(denom_ng7 > 0)
    ).round(4)

    annual["regime_share_g7"] = (
        annual["regime_g7"] / denom_g7.where(denom_g7 > 0)
    ).round(4)

    annual["gap_pp"] = (
        (annual["regime_share_non_g7"] - annual["regime_share_g7"]) * 100
    ).round(2)

    annual.to_csv(OUTPUT_ANNUAL, index=False)
    log.info(f"Annual summary -> {OUTPUT_ANNUAL}")

    print("\n-- FILES USED PER YEAR -----------------------------------------")
    print(annual[["year", "files_used"]].to_string(index=False))
    print("\n-- RAW COUNTS --------------------------------------------------")
    print(annual[["year", "regime_non_g7", "regime_g7",
                  "govt_non_g7", "govt_g7"]].to_string(index=False))
    print("\n-- OCCURRENCE INDEX --------------------------------------------")
    print(annual[["year", "regime_share_non_g7",
                  "regime_share_g7", "gap_pp"]].to_string(index=False))

    # ── CHART ────────────────────────────────────────────────────────────────

    fig, axes = plt.subplots(2, 2, figsize=(15, 10))
    fig.suptitle(
        '"Regime" vs "Government / Administration / Leadership"\n'
        f'Western media: G7 vs Non-G7 authoritarian states  '
        f'(GDELT GKG 2.0, {YEARS[0]}-{YEARS[-1]})\n'
        'Non-G7: Russia, China, Iran, North Korea, Venezuela, '
        'Saudi Arabia, Belarus, Syria, Cuba, Myanmar',
        fontsize=11, fontweight="bold"
    )

    yrs   = annual["year"].tolist()
    ng_r  = annual["regime_non_g7"].tolist()
    g7_r  = annual["regime_g7"].tolist()
    ng_g  = annual["govt_non_g7"].tolist()
    g7_g  = annual["govt_g7"].tolist()
    ng_sh = (annual["regime_share_non_g7"] * 100).tolist()
    g7_sh = (annual["regime_share_g7"]     * 100).tolist()
    gap   = annual["gap_pp"].tolist()
    n_files = annual["files_used"].tolist()

    # Top-left: raw regime counts
    ax = axes[0][0]
    ax.plot(yrs, ng_r, "o-", color="#c0392b", lw=2, label="Non-G7")
    ax.plot(yrs, g7_r, "s--", color="#2980b9", lw=2, label="G7")
    ax.set_title('Raw "regime" article hits', fontsize=11)
    ax.set_ylabel("Article mentions")
    ax.set_xlabel("Year")
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_locator(plt.MaxNLocator(integer=True))

    # Top-right: raw govt counts + files-used annotation
    ax2 = axes[0][1]
    ax2.plot(yrs, ng_g, "o-", color="#e67e22", lw=2, label="Non-G7")
    ax2.plot(yrs, g7_g, "s--", color="#27ae60", lw=2, label="G7")
    for x, y, n in zip(yrs, ng_g, n_files):
        ax2.annotate(f"n={n}", (x, y), textcoords="offset points",
                     xytext=(0, 6), ha="center", fontsize=7,
                     color="#777777")
    ax2.set_title('Raw "government / administration / leadership" hits\n'
                  '(n = files used per year)', fontsize=10)
    ax2.set_ylabel("Article mentions")
    ax2.set_xlabel("Year")
    ax2.legend(fontsize=9)
    ax2.grid(True, alpha=0.3)
    ax2.xaxis.set_major_locator(plt.MaxNLocator(integer=True))

    # Bottom-left: occurrence index
    ax3 = axes[1][0]
    ax3.plot(yrs, ng_sh, "o-", color="#c0392b", lw=2.5, label="Non-G7")
    ax3.plot(yrs, g7_sh, "s--", color="#2980b9", lw=2.5, label="G7")
    ax3.set_title(
        'Occurrence index: "regime" share\n'
        'regime / (regime + govt/admin/leadership)',
        fontsize=10
    )
    ax3.set_ylabel("Occurrence index (%)")
    ax3.set_xlabel("Year")
    ax3.yaxis.set_major_formatter(mtick.PercentFormatter())
    ax3.legend(fontsize=9)
    ax3.grid(True, alpha=0.3)
    ax3.xaxis.set_major_locator(plt.MaxNLocator(integer=True))

    # Bottom-right: gap
    ax4 = axes[1][1]
    colors = ["#e74c3c" if v >= 0 else "#2980b9" for v in gap]
    bars = ax4.bar(yrs, gap, color=colors, edgecolor="white", width=0.6)
    ax4.set_title(
        "Gap: Non-G7 minus G7 occurrence index (pp)\n"
        "Positive = Non-G7 labelled 'regime' more than G7",
        fontsize=10
    )
    ax4.set_ylabel("Percentage-point difference")
    ax4.set_xlabel("Year")
    ax4.axhline(0, color="black", lw=0.8)
    ax4.grid(True, alpha=0.3, axis="y")
    ax4.xaxis.set_major_locator(plt.MaxNLocator(integer=True))
    for bar, val in zip(bars, gap):
        ypos = bar.get_height() + (0.3 if val >= 0 else -1.5)
        ax4.text(
            bar.get_x() + bar.get_width() / 2, ypos,
            f"{val:+.1f}pp", ha="center", va="bottom", fontsize=8
        )

    plt.tight_layout()
    plt.savefig(OUTPUT_CHART, dpi=150)
    log.info(f"Chart saved -> {OUTPUT_CHART}")
    plt.show()
    log.info("Done.")


if __name__ == "__main__":
    run()
