"""
update_report.py
----------------
Runs on a daily GitHub Actions schedule.

SOURCE ARCHITECTURE:
  Live report sources (change-detected, trigger Claude when updated):
    - DSO (Due South Outfitters): two-step fetch — index → latest article
    - FlyLifeOutdoors Blog: two-step fetch — index → latest article
    - FlyFishingNC: single fetch attempt (JS-rendered, may return sparse text)

  Static context sources (always fetched, provide background for Claude):
    - FlyLifeOutdoors Watauga County page
    - FlyLifeOutdoors Ashe County page
    - FlyLifeOutdoors Avery County page (if available)

LOGIC:
  1. Fetch and fingerprint all live report sources
  2. If nothing changed vs. stored hashes → exit, no Claude call, no cost
  3. If anything changed → fetch static context pages, combine everything,
     send to Claude, write report.json, update hashes

Requires env var: ANTHROPIC_API_KEY
"""

import os
import json
import hashlib
import re
import requests
from datetime import date
from bs4 import BeautifulSoup
import anthropic

# ── paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT   = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
HASH_FILE   = os.path.join(SCRIPT_DIR, "report_hashes.json")
REPORT_FILE = os.path.join(REPO_ROOT, "report.json")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; WNCFishingReportBot/1.0)"}

# ── fly image map ─────────────────────────────────────────────────────────────
# Maps lowercase fly name keywords → image filename in your /flies folder.
# Add new entries here whenever you add images to the repo.
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
    """Raw fetch → BeautifulSoup. Raises on HTTP error."""
    r = requests.get(url, timeout=timeout, headers=HEADERS)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def soup_to_text(soup: BeautifulSoup, char_limit: int = 5000) -> str:
    """Strip chrome elements, return plain text capped at char_limit."""
    for tag in soup(["nav", "footer", "header", "script", "style", "aside",
                     "form", "button", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    # collapse excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:char_limit]


def fetch_text_simple(url: str, char_limit: int = 5000) -> str:
    """Single-step fetch → clean text. Used for static reference pages."""
    soup = fetch_page(url)
    return soup_to_text(soup, char_limit)


def fetch_latest_article(index_url: str,
                          link_pattern: str,
                          char_limit: int = 5000) -> tuple[str, str]:
    """
    Two-step fetch:
      1. Load index page, find the first <a> whose href matches link_pattern
      2. Fetch that article URL and return (article_url, article_text)

    link_pattern is a regex matched against href strings.
    Returns ("", "") if no matching link found.
    """
    soup = fetch_page(index_url)
    # find first matching link
    article_url = ""
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(link_pattern, href):
            # make absolute if relative
            if href.startswith("/"):
                from urllib.parse import urlparse
                parsed = urlparse(index_url)
                href = f"{parsed.scheme}://{parsed.netloc}{href}"
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
    """Return image filename for a fly name, or empty string if unknown."""
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
    """
    report_text  — scraped content from live report sources (DSO, FLO blog, FFNC)
    context_text — scraped content from static reference pages (county stream guides)
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    schema = {
        "updated": "YYYY-MM-DD",
        "source": "Due South Outfitters + Fly Life Outdoors",
        "source_url": "https://duesouthoutfitters.com/due-south-outfitters-fly-fishing-report/",
        "conditions": {
            "overall": "1–2 sentence plain-language summary of current conditions across WNC High Country",
            "flow": "low | normal | high",
            "clarity": "clear | stained | turbid",
            "temp": "cold | cool | warm"
        },
        "top_flies": [
            {
                "name": "Fly name as it appears on fly shop shelves",
                "size": "#16-18",
                "img": "",
                "note": "When, where, and how to fish it — be specific to WNC waters"
            }
        ],
        "tactics": [
            "One actionable tactic per string — WNC-specific, no generic advice"
        ],
        "waters": [
            {
                "name": "Stream or lake name",
                "note": "Current conditions or fishing tip for this specific water"
            }
        ],
        "elevation_note": "Brief note that High Country streams (Boone area, 3,300–4,000 ft) run 1–2 weeks behind lower-elevation WNC",
        "stocking_alert": False,
        "stocking_note": "Stocking news if stocking_alert is true — otherwise empty string"
    }

    prompt = f"""You are synthesizing a weekly fly fishing report for anglers fishing \
Watauga, Ashe, and Avery Counties in the NC High Country (~3,300–4,000 ft elevation).

Your job is to triangulate across multiple sources and produce the single best \
recommendation set for a local angler heading out this week. Where sources agree, \
that's high-confidence intel. Where they differ, favor the most recent or most \
WNC-specific source (DSO is the gold standard for this region).

Return ONLY a valid JSON object matching this exact schema. \
No markdown, no explanation, no code fences — raw JSON only:

{json.dumps(schema, indent=2)}

RULES:
- top_flies: 5–8 flies. Include only patterns actually mentioned or strongly implied \
by the reports. Use hook sizes from the source when given.
- img: always leave as empty string "" — the pipeline fills this in automatically.
- flow / clarity / temp: use ONLY the exact enum values shown in the schema.
- tactics: 4–7 bullets. Actionable and specific — tippet sizes, rig types, time of day, \
presentation style. No filler.
- waters: only waters explicitly named in the reports.
- stocking_alert: true only if a stocking event is mentioned as current or imminent.
- elevation_note: always include — it's important context for High Country timing.
- source: list which sources contributed (e.g. "Due South Outfitters + Fly Life Outdoors").
- source_url: use DSO's report URL as the primary link.
- updated: use today's date → {date.today().isoformat()}
- If a field has no data, use "" or [] — never omit a key.

━━━ LIVE REPORT SOURCES (primary intel — time-sensitive) ━━━
{report_text}

━━━ STATIC CONTEXT SOURCES (background reference — stocking totals, stream types, regs) ━━━
{context_text}"""

    response = client.messages.create(
        model="claude-opus-4-20250514",
        max_tokens=1800,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()

    # strip markdown fences if the model added them anyway
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    return json.loads(raw)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("=== WNC Fishing Report Updater ===")
    print(f"Date: {date.today().isoformat()}\n")

    old_hashes = load_hashes()
    new_hashes  = {}

    # ── LIVE REPORT SOURCES ──────────────────────────────────────────────────
    # These are change-detected. Claude only runs if at least one has updated.

    live_sources = {}  # key → (label, fetched_text, article_url)

    # 1. DSO — two-step: index → latest monthly report article
    print("Fetching DSO...")
    try:
        dso_index = "https://duesouthoutfitters.com/due-south-outfitters-fly-fishing-report/"
        # DSO article URLs follow pattern: /western-north-carolina-east-tennessee-fly-fishing-report-*/
        article_url, text = fetch_latest_article(
            dso_index,
            link_pattern=r"fly-fishing-report-(?!/$)[a-z]"
        )
        if text:
            print(f"  DSO article: {article_url}")
            live_sources["DSO"] = ("Due South Outfitters", text, article_url)
        else:
            print("  DSO: no article link found on index page")
    except Exception as e:
        print(f"  DSO: FAILED — {e}")

    # 2. FlyLifeOutdoors Blog — two-step: blog index → latest post
    print("Fetching FlyLifeOutdoors blog...")
    try:
        flo_index = "https://flylifeoutdoors.com/blogs/on-the-water"
        article_url, text = fetch_latest_article(
            flo_index,
            link_pattern=r"/blogs/on-the-water/[a-z]"
        )
        if text:
            print(f"  FLO article: {article_url}")
            live_sources["FLO"] = ("Fly Life Outdoors Blog", text, article_url)
        else:
            print("  FLO blog: no article link found — using index text")
            text = fetch_text_simple(flo_index, char_limit=3000)
            live_sources["FLO"] = ("Fly Life Outdoors Blog", text, flo_index)
    except Exception as e:
        print(f"  FLO blog: FAILED — {e}")

    # 3. FlyFishingNC — single fetch (may be JS-rendered; accept gracefully)
    print("Fetching FlyFishingNC...")
    try:
        text = fetch_text_simple("https://www.flyfishingnc.com/fly-fishing-reports", char_limit=3000)
        if len(text) > 200:  # if we got something useful
            live_sources["FFNC"] = ("FlyFishingNC", text, "https://www.flyfishingnc.com/fly-fishing-reports")
            print(f"  FlyFishingNC: {len(text)} chars fetched")
        else:
            print("  FlyFishingNC: too sparse (likely JS-rendered) — skipping")
    except Exception as e:
        print(f"  FlyFishingNC: FAILED — {e}")

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
            print(f"  {key}: no change — skipping")

    if not changed_sources:
        print("\nNo sources changed. Skipping Claude call. report.json untouched.")
        return

    # ── STATIC CONTEXT SOURCES ───────────────────────────────────────────────
    # Always fetched when we're going to call Claude anyway — cheap HTTP requests,
    # gives Claude the stocking/regulation background it needs for richer output.
    print("\nFetching static context pages...")

    context_parts = []
    static_pages = [
        ("Watauga County Streams", "https://flylifeoutdoors.com/pages/watauga-county-nc-trout-streams"),
        ("Ashe County Streams",    "https://flylifeoutdoors.com/pages/ashe-county-nc-trout-streams"),
        ("Avery County Streams",   "https://flylifeoutdoors.com/pages/avery-county-nc-trout-streams"),
    ]

    for label, url in static_pages:
        try:
            text = fetch_text_simple(url, char_limit=2500)
            context_parts.append(f"--- {label} ({url}) ---\n{text}")
            print(f"  {label}: OK ({len(text)} chars)")
        except Exception as e:
            print(f"  {label}: FAILED — {e}")

    context_text = "\n\n".join(context_parts) if context_parts else "(no static context available)"

    # ── BUILD REPORT TEXT FOR CLAUDE ─────────────────────────────────────────
    report_parts = []
    primary_url  = ""

    for key, (label, text, url) in changed_sources.items():
        report_parts.append(f"--- SOURCE: {label}\n--- URL: {url}\n\n{text}")
        if key == "DSO":
            primary_url = url  # DSO gets the source_url field

    # also include unchanged live sources as supporting context
    for key, (label, text, url) in live_sources.items():
        if key not in changed_sources:
            report_parts.append(f"--- SOURCE (unchanged but included for context): {label}\n--- URL: {url}\n\n{text[:2000]}")

    if not primary_url and live_sources.get("DSO"):
        primary_url = live_sources["DSO"][2]
    if not primary_url:
        primary_url = "https://duesouthoutfitters.com/due-south-outfitters-fly-fishing-report/"

    report_text = "\n\n".join(report_parts)

    # ── CALL CLAUDE ───────────────────────────────────────────────────────────
    source_names = " + ".join(label for label, _, _ in changed_sources.values())
    print(f"\nCalling Claude API — sources: {source_names}")

    report = call_claude(report_text, context_text)

    # override source_url with the actual DSO article URL if we have it
    if primary_url:
        report["source_url"] = primary_url

    # ── INJECT IMAGES ─────────────────────────────────────────────────────────
    report = inject_images(report)

    # ── WRITE report.json ─────────────────────────────────────────────────────
    with open(REPORT_FILE, "w") as f:
        json.dump(report, f, indent=2)
    print(f"  report.json written — updated: {report.get('updated')}")
    print(f"  flies: {len(report.get('top_flies', []))}, waters: {len(report.get('waters', []))}")

    # ── SAVE HASHES ───────────────────────────────────────────────────────────
    save_hashes(new_hashes)
    print("  report_hashes.json updated")
    print("\nDone. ✓")


if __name__ == "__main__":
    main()
