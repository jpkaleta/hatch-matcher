"""
update_report.py
----------------
Runs on a weekly GitHub Actions schedule (Thursdays).

SOURCE ARCHITECTURE:
  Live report sources (change-detected — Claude only runs if something changed):
    - DSO: tries current month URL, previous month URL, then index fallback
    - FlyLifeOutdoors Blog: two-step fetch index -> latest article
    - FlyFishingNC: single fetch (JS-rendered, may be sparse)

  Static context sources (fetched when Claude runs):
    - FlyLifeOutdoors Watauga, Ashe, Avery county pages

IMAGE MATCHING:
  No hardcoded dictionary. Script fetches the live /flies directory from GitHub,
  then a second Claude call matches fly names to actual filenames using common
  sense — handles variants, spelling differences, color suffixes automatically.
  New images added to the repo are picked up on the next run with no code changes.

Requires env var: ANTHROPIC_API_KEY
Requires env var: GITHUB_REPOSITORY  (set automatically by GitHub Actions, e.g. "jpkaleta/hatch-matcher")
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

# GitHub repo slug — auto-set by Actions, fallback for local testing
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "jpkaleta/hatch-matcher")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
    # Accept-Encoding intentionally omitted — lets requests handle decompression automatically
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
# GITHUB IMAGE DISCOVERY
# ─────────────────────────────────────────────────────────────────────────────

def get_fly_image_filenames(repo: str) -> list[str]:
    """
    Fetch the current list of image filenames from the /flies directory
    via the GitHub API. Works on public repos with no auth required.
    Returns sorted list of .png filenames.
    """
    url = f"https://api.github.com/repos/{repo}/contents/flies"
    r = requests.get(url, timeout=15, headers={"User-Agent": "WNCHatchMatcher/1.0"})
    r.raise_for_status()
    files = [f["name"] for f in r.json()
             if isinstance(f, dict) and f.get("name", "").lower().endswith(".png")]
    files.sort()
    print(f"  Found {len(files)} fly images in repo")
    return files


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
# CLAUDE API CALLS
# ─────────────────────────────────────────────────────────────────────────────

def call_claude_report(report_text: str, context_text: str, today: str) -> dict:
    """
    Call 1 of 2: synthesize fishing report sources into structured JSON.
    Image fields are left empty — call_claude_images() fills them in.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

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

    prompt = f"""Extract fly fishing intel from the reports below and return it as a single JSON object.

You are building a weekly report for anglers in Watauga, Ashe, and Avery Counties, NC (~3,300-4,000 ft elevation).
DSO (Due South Outfitters) is the primary source — their fly recommendations and conditions notes take priority.

Return ONLY the JSON object. No markdown fences, no explanation, no preamble.
Your entire response must be valid JSON starting with {{ and ending with }}.

Populate this schema exactly:
{json.dumps(schema, indent=2)}

Field instructions:
- conditions.overall: 1-2 sentences summarizing current conditions from the reports
- conditions.flow: exactly "low", "normal", or "high"
- conditions.clarity: exactly "clear", "stained", or "turbid"
- conditions.temp: exactly "cold", "cool", or "warm"
- top_flies: all fly patterns mentioned in the reports with their hook sizes. 5-8 flies. Leave img as "".
- tactics: the specific pro tips and fishing advice from the reports. 4-7 bullets.
- waters: each stream or water body mentioned, with its specific conditions or tip.
- stocking_alert: true if any stocking event is mentioned as recent, current, or coming soon.
- elevation_note: note that High Country streams near Boone (3,300-4,000 ft) run 1-2 weeks behind lower WNC.
- updated: {today}
- If a field has no data, use "" or [] — never omit a key from the schema.

━━━ REPORTS ━━━
{report_text}

━━━ BACKGROUND CONTEXT ━━━
{context_text}"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2500,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()
    print(f"  Report response ({len(raw)} chars): {raw[:150]}...")

    # strip markdown fences robustly — handles ```json ... ``` and ``` ... ```
    raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\n?```$", "", raw)
    raw = raw.strip()

    if not raw:
        raise ValueError("Claude returned an empty response for the report")

    return json.loads(raw)


def call_claude_images(flies: list[dict], filenames: list[str]) -> list[dict]:
    """
    Call 2 of 2: match each fly name to the closest image filename.
    Uses common sense — handles color variants, spelling differences,
    regional name variations, and partial matches automatically.
    Returns the flies list with img fields populated.
    """
    if not filenames:
        print("  No image filenames available — skipping image matching")
        return flies

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    fly_names = [f["name"] for f in flies]

    prompt = f"""You are matching fly fishing pattern names to the closest available image files.

Use common sense for matching:
- Color variants are fine: "Elk Hair Caddis" matches "Elk-Hair-Caddis-Olive.png"
- Spelling variations: "Sulfur" matches "Sulphur", "Wooly" matches "Woolly"  
- Partial names: "Zebra Midge" matches "Zebra-Midge-Black.png"
- Regional names: "Yellow Sally" matches "Stimulator-Yellow.png"
- If multiple files could match, pick the most natural one
- If truly nothing is close, use ""

Fly patterns to match:
{json.dumps(fly_names, indent=2)}

Available image files:
{json.dumps(filenames, indent=2)}

Return ONLY a JSON array of strings — one filename (or "") per fly, in the exact same order as the input list.
No explanation, no markdown, just the raw JSON array starting with [ and ending with ]."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()
    print(f"  Image match response: {raw}")

    # strip markdown fences robustly
    raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\n?```$", "", raw)
    raw = raw.strip()

    matches = json.loads(raw)

    for fly, img in zip(flies, matches):
        fly["img"] = img if img else ""

    return flies


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("=== WNC Fishing Report Updater ===")
    today = datetime.date.today()
    print(f"Date: {today.isoformat()}")
    print(f"Repo: {GITHUB_REPO}\n")

    old_hashes   = load_hashes()
    new_hashes   = {}
    live_sources = {}   # key -> (label, text, url)

    # ── 1. DSO ────────────────────────────────────────────────────────────────
    print("Fetching DSO...")
    try:
        month_name      = today.strftime("%B").lower()
        prev_dt         = today.replace(day=1) - datetime.timedelta(days=1)
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
                    print(f"  DSO OK: {len(t)} chars")
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
            print("  FLO: no article found — using index")
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
        print("\nNo sources changed. Skipping Claude calls.")
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

    # ── FETCH FLY IMAGE LIST FROM GITHUB ─────────────────────────────────────
    print("\nFetching fly image list from GitHub...")
    try:
        fly_filenames = get_fly_image_filenames(GITHUB_REPO)
    except Exception as e:
        print(f"  WARNING: could not fetch image list — {e}")
        fly_filenames = []

    # ── BUILD REPORT TEXT ─────────────────────────────────────────────────────
    primary_url  = ""
    report_parts = []

    for key, (label, text, url) in changed_sources.items():
        report_parts.append(f"--- SOURCE: {label}\n--- URL: {url}\n\n{text}")
        if key == "DSO":
            primary_url = url

    for key, (label, text, url) in live_sources.items():
        if key not in changed_sources:
            report_parts.append(f"--- CONTEXT (unchanged): {label}\n\n{text[:2000]}")

    if not primary_url:
        primary_url = live_sources.get("DSO", ("", "", "https://duesouthoutfitters.com/due-south-outfitters-fly-fishing-report/"))[2]

    report_text  = "\n\n".join(report_parts)
    source_names = " + ".join(label for label, _, _ in changed_sources.values())

    # ── CALL 1: SYNTHESIZE REPORT ─────────────────────────────────────────────
    print(f"\nCalling Claude (report synthesis) — sources: {source_names}")
    # DEBUG: uncomment to see raw scraped text sent to Claude (useful when output looks hallucinated)
    # print(f"\n--- REPORT TEXT SENT TO CLAUDE ({len(report_text)} chars) ---")
    # print(report_text[:3000])
    # print("--- END REPORT TEXT ---\n")
    report = call_claude_report(report_text, context_text, today.isoformat())

    if primary_url:
        report["source_url"] = primary_url

    # ── CALL 2: MATCH FLY IMAGES ──────────────────────────────────────────────
    if fly_filenames and report.get("top_flies"):
        print(f"\nCalling Claude (image matching) — {len(report['top_flies'])} flies, {len(fly_filenames)} images")
        report["top_flies"] = call_claude_images(report["top_flies"], fly_filenames)
    else:
        print("\nSkipping image matching (no flies or no filenames available)")

    # ── WRITE OUTPUT ──────────────────────────────────────────────────────────
    with open(REPORT_FILE, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n  report.json written — updated: {report.get('updated')}")
    print(f"  flies: {len(report.get('top_flies', []))}, waters: {len(report.get('waters', []))}")

    save_hashes(new_hashes)
    print("  report_hashes.json updated")
    print("\nDone. ✓")


if __name__ == "__main__":
    main()
