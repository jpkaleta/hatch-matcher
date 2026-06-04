"""
update_report.py
----------------
Runs on a daily GitHub Actions schedule.

SOURCE ARCHITECTURE:
  Live report sources (change-detected, trigger Claude when updated):
    - DSO: tries current month URL, previous month URL, then index fallback
    - FlyLifeOutdoors Blog: two-step fetch index -> latest article
    - FlyFishingNC: single fetch (JS-rendered, may be sparse)

  Static context sources (always fetched when Claude runs):
    - FlyLifeOutdoors Watauga, Ashe, Avery county pages

Requires env var: ANTHROPIC_API_KEY
"""

import os
import json
import hashlib
import re
import datetime
import requests
from bs4 import BeautifulSoup
import anthropic

# ── paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT   = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
HASH_FILE   = os.path.join(SCRIPT_DIR, "report_hashes.json")
REPORT_FILE = os.path.join(REPO_ROOT, "report.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

# ── fly image map ─────────────────────────────────────────────────────────────
FLY_IMAGE_MAP = {
    "sulphur dry":                   "Sulphur-Dun.png",
    "sulphur dun":                   "Sulphur-Dun.png",
    "sulphur nymph":                 "Sulphur-Nymph.png",
    "yellow sally":                  "Stimulator-Yellow.png",
    "yellow rubber leg stimulator":  "Stimulator-Yellow.png",
    "stimulator":                    "Stimulator-Yellow.png",
    "frenchie":                      "Frenchie.png",
    "zebra midge":                   "Zebra-Midge-Black.png",
    "chubby chernobyl":              "Chubby-Chernobyl-Golden-Stone.png",
    "elk hair caddis":               "Elk-Hair-Caddis-Olive.png",
    "parachute adams":               "Parachute-Adams.png",
    "adams":                         "Parachute-Adams.png",
    "pheasant tail":                 "Pheasant-Tail-Nymph.png",
    "hares ear":                     "Hares-Ear.png",
    "san juan worm":                 "San-Juan-Worm-Red.png",
    "squirmy worm":                  "Squirmy-Worm-Pink.png",
    "egg pattern":                   "Egg-Oregon-Cheese.png",
    "eggs":                          "Egg-Oregon-Cheese.png",
    "bwo nymph":                     "BWO-Nymph.png",
    "blue winged olive":             "BWO-Nymph.png",
    "blue-winged olive":             "BWO-Nymph.png",
    "soft hackle":                   "Soft-Hackle-Partridge-Orange.png",
    "duracell":                      "Duracell.png",
    "waltz worm":                    "Waltz-Worm.png",
    "green drake":                   "Green-Drake.png",
    "isonychia":                     "Isonychia.png",
    "light cahill":                  "Light-Cahill.png",
    "quill gordon":                  "Quill-Gordon.png",
    "woolly bugger":                 "Woolly-Bugger-Olive.png",
    "wooly bugger":                  "Woolly-Bugger-Olive.png",
    "prince nymph":                  "Prince-Nymph.png",
    "prince":                        "Prince-Nymph.png",
    "copper john":                   "Copper-John.png",
    "royal wulff":                   "Royal-Wulff.png",
    "hendrickson":                   "Hendrickson.png",
    "march brown":                   "March-Brown.png",
}


# ─────────────────────────────────────────────────────────────────────────────
# FETCH UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def fetch_page(url: str, timeout: int = 20) -> BeautifulSoup:
    r = requests.get(url, timeout=timeout, headers=HEADERS)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def soup_to_text(soup: BeautifulSoup, char_limit: int = 5000) -> str:
    for tag in soup(["nav", "footer", "header", "script", "style", "aside",
                     "form", "button", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:char_limit]


def fetch_text_simple(url: str, char_limit: int = 5000) -> str:
    soup = fetch_page(url)
    return soup_to_text(soup, char_limit)


def fetch_latest_article(index_url: str, link_pattern: str,
                          char_limit: int = 5000) -> tuple[str, str]:
    """Index page -> find first link matching pattern -> fetch article."""
    soup = fetch_page(index_url)
    article_url = ""
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(link_pattern, href):
            if href.startswith("/"):
                from urllib.parse import urlparse
                p = urlparse(index_url)
                href = f"{p.scheme}://{p.netloc}{href}"
            article_url = href
            break
    if not article_url:
        return "", ""
    article_soup = fetch_page(article_url)
    return article_url, soup_to_text(article_soup, char_limit)


# ─────────────────────────────────────────────────────────────────────────────
# HASHING
# ─────────────────────────────────────────────────────────────────────────────

def fingerprint(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def load_hashes() -> dict:
    if os.path.exists(HASH_FILE):
        with open(HASH_FILE) as f:
            return json.load(f)
    return {}


def save_hashes(hashes: dict) -> None:
    with open(HASH_FILE, "w") as f:
        json.dump(hashes, f, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# IMAGE INJECTION
# ─────────────────────────────────────────────────────────────────────────────

def resolve_image(fly_name: str) -> str:
    key = fly_name.lower().strip()
    if key in FLY_IMAGE_MAP:
        return FLY_IMAGE_MAP[key]
    for map_key, filename in FLY_IMAGE_MAP.items():
        if map_key in key or key in map_key:
            return filename
    return ""


def inject_images(report: dict) -> dict:
    for fly in report.get("top_flies", []):
        if not fly.get("img"):
            fly["img"] = resolve_image(fly.get("name", ""))
    return report


# ─────────────────────────────────────────────────────────────────────────────
# CLAUDE API CALL
# ─────────────────────────────────────────────────────────────────────────────

def call_claude(report_text: str, context_text: str) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    today = datetime.date.today().isoformat()

    schema = {
        "updated": today,
        "source": "Due South Outfitters + Fly Life Outdoors",
        "source_url": "https://duesouthoutfitters.com/due-south-outfitters-fly-fishing-report/",
        "conditions": {
            "overall": "1-2 sentence summary of current conditions",
            "flow": "low",
            "clarity": "clear",
            "temp": "cool"
        },
        "top_flies": [
            {"name": "Fly Name", "size": "#16-18", "img": "", "note": "When/where/how"}
        ],
        "tactics": ["One actionable tactic per string"],
        "waters": [{"name": "Stream name", "note": "Current tip for this water"}],
        "elevation_note": "High Country streams (Boone, 3300-4000ft) run 1-2 weeks behind lower-elevation WNC",
        "stocking_alert": False,
        "stocking_note": ""
    }

    prompt = f"""You are synthesizing a weekly fly fishing report for Watauga, Ashe, and Avery Counties, NC High Country (~3,300-4,000 ft elevation).

Triangulate across all sources and return the single best recommendation set for a local angler.
Where sources agree = high confidence. Favor DSO when sources conflict — it is most WNC-specific.

YOU MUST RETURN ONLY A VALID JSON OBJECT. No explanation, no markdown fences, no preamble.
Start your response with {{ and end with }}. Nothing else.

Use this exact schema:
{json.dumps(schema, indent=2)}

Rules:
- flow: only "low", "normal", or "high"
- clarity: only "clear", "stained", or "turbid"  
- temp: only "cold", "cool", or "warm"
- top_flies: 5-8 flies mentioned in the reports, with hook sizes
- img: always leave as "" (pipeline fills this)
- tactics: 4-7 short actionable bullets, WNC-specific
- waters: only waters explicitly named in the reports
- stocking_alert: true only if stocking is current or imminent
- updated: {today}
- If no data for a field: use "" or [] but never omit the key

━━━ LIVE REPORT SOURCES ━━━
{report_text}

━━━ BACKGROUND CONTEXT (stocking numbers, stream types) ━━━
{context_text}"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1800,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()
    print(f"  Claude raw response ({len(raw)} chars): {raw[:200]}...")

    # strip markdown fences if present
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    if not raw:
        raise ValueError("Claude returned an empty response")

    return json.loads(raw)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("=== WNC Fishing Report Updater ===")
    today = datetime.date.today()
    print(f"Date: {today.isoformat()}\n")

    old_hashes  = load_hashes()
    new_hashes  = {}
    live_sources = {}  # key -> (label, text, url)

    # ── 1. DSO ────────────────────────────────────────────────────────────────
    # Try current month article, then previous month, then index page fallback.
    print("Fetching DSO...")
    try:
        month_name      = today.strftime("%B").lower()
        prev_dt         = (today.replace(day=1) - datetime.timedelta(days=1))
        prev_month_name = prev_dt.strftime("%B").lower()
        dso_base        = "https://duesouthoutfitters.com/western-north-carolina-east-tennessee-fly-fishing-report-"
        dso_index       = "https://duesouthoutfitters.com/due-south-outfitters-fly-fishing-report/"

        dso_text = ""
        dso_url  = ""
        for candidate in [f"{dso_base}{month_name}/", f"{dso_base}{prev_month_name}/"]:
            try:
                print(f"  trying: {candidate}")
                t = fetch_text_simple(candidate, char_limit=5000)
                if len(t) > 300:
                    dso_text, dso_url = t, candidate
                    print(f"  DSO OK: {len(t)} chars from {candidate}")
                    break
            except Exception as e:
                print(f"  {candidate} failed: {e}")

        if not dso_text:
            print("  DSO: falling back to index page")
            dso_text = fetch_text_simple(dso_index, char_limit=5000)
            dso_url  = dso_index
            print(f"  DSO index: {len(dso_text)} chars")

        live_sources["DSO"] = ("Due South Outfitters", dso_text, dso_url)

    except Exception as e:
        print(f"  DSO FAILED: {e}")

    # ── 2. FlyLifeOutdoors Blog ───────────────────────────────────────────────
    print("Fetching FlyLifeOutdoors blog...")
    try:
        flo_index = "https://flylifeoutdoors.com/blogs/on-the-water"
        url, text = fetch_latest_article(flo_index, link_pattern=r"/blogs/on-the-water/[a-z]")
        if text:
            print(f"  FLO article: {url} ({len(text)} chars)")
            live_sources["FLO"] = ("Fly Life Outdoors Blog", text, url)
        else:
            print("  FLO: no article link found — using index text")
            t = fetch_text_simple(flo_index, char_limit=3000)
            live_sources["FLO"] = ("Fly Life Outdoors Blog", t, flo_index)
    except Exception as e:
        print(f"  FLO FAILED: {e}")

    # ── 3. FlyFishingNC ───────────────────────────────────────────────────────
    print("Fetching FlyFishingNC...")
    try:
        t = fetch_text_simple("https://www.flyfishingnc.com/fly-fishing-reports", char_limit=3000)
        if len(t) > 200:
            print(f"  FlyFishingNC: {len(t)} chars")
            live_sources["FFNC"] = ("FlyFishingNC", t, "https://www.flyfishingnc.com/fly-fishing-reports")
        else:
            print("  FlyFishingNC: too sparse, skipping")
    except Exception as e:
        print(f"  FlyFishingNC FAILED: {e}")

    # ── CHANGE DETECTION ─────────────────────────────────────────────────────
    print("\nChecking for changes...")
    changed_sources = {}
    for key, (label, text, url) in live_sources.items():
        h = fingerprint(text)
        new_hashes[key] = h
        if old_hashes.get(key) != h:
            print(f"  {key}: CHANGED ✓")
            changed_sources[key] = (label, text, url)
        else:
            print(f"  {key}: no change")

    if not changed_sources:
        print("\nNo sources changed. Skipping Claude call.")
        return

    # ── STATIC CONTEXT ────────────────────────────────────────────────────────
    print("\nFetching static context pages...")
    context_parts = []
    for label, url in [
        ("Watauga County Streams", "https://flylifeoutdoors.com/pages/watauga-county-nc-trout-streams"),
        ("Ashe County Streams",    "https://flylifeoutdoors.com/pages/ashe-county-nc-trout-streams"),
        ("Avery County Streams",   "https://flylifeoutdoors.com/pages/avery-county-nc-trout-streams"),
    ]:
        try:
            t = fetch_text_simple(url, char_limit=2500)
            context_parts.append(f"--- {label} ---\n{t}")
            print(f"  {label}: OK ({len(t)} chars)")
        except Exception as e:
            print(f"  {label}: FAILED — {e}")

    context_text = "\n\n".join(context_parts) or "(no static context available)"

    # ── BUILD REPORT TEXT ─────────────────────────────────────────────────────
    primary_url  = ""
    report_parts = []

    for key, (label, text, url) in changed_sources.items():
        report_parts.append(f"--- SOURCE: {label}\n--- URL: {url}\n\n{text}")
        if key == "DSO":
            primary_url = url

    # include unchanged sources as supporting context at reduced length
    for key, (label, text, url) in live_sources.items():
        if key not in changed_sources:
            report_parts.append(f"--- CONTEXT (unchanged): {label}\n\n{text[:2000]}")

    if not primary_url:
        primary_url = live_sources.get("DSO", ("", "", "https://duesouthoutfitters.com/due-south-outfitters-fly-fishing-report/"))[2]

    report_text   = "\n\n".join(report_parts)
    source_names  = " + ".join(label for label, _, _ in changed_sources.values())

    # ── CALL CLAUDE ───────────────────────────────────────────────────────────
    print(f"\nCalling Claude API — sources: {source_names}")
    report = call_claude(report_text, context_text)

    if primary_url:
        report["source_url"] = primary_url

    report = inject_images(report)

    # ── WRITE OUTPUT ──────────────────────────────────────────────────────────
    with open(REPORT_FILE, "w") as f:
        json.dump(report, f, indent=2)
    print(f"  report.json written — updated: {report.get('updated')}")
    print(f"  flies: {len(report.get('top_flies', []))}, waters: {len(report.get('waters', []))}")

    save_hashes(new_hashes)
    print("  report_hashes.json updated")
    print("\nDone. ✓")


if __name__ == "__main__":
    main()
