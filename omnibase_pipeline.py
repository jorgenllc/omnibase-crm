"""
omnibase_pipeline.py — Unified OmniBase enrichment pipeline

Merges Lead Machine's crawl + DeepSeek enrichment with OmniBase's cleansing,
Record-ID, and relational-output logic into ONE callable.

    raw scrape CSV
        │  1. read rows, detect columns (url, reviews, etc.)
        │  2. crawl each url (crawl4ai, concurrency-limited)
        │  3. DeepSeek extract structured fields (OpenRouter, strict JSON)
        │  4. tech/marketing fingerprint scan of crawled source
        │  5. assemble enriched row → append enriched_leads.csv (audit trail)
        │  6. cleanse: city/state/zip parse, hygiene, deterministic Record ID
        │  7. company-only routing → companies.csv (update-in-place by Record ID)
        ▼
    data/companies.csv   (+ contacts.csv stub)

Public entry point:
    enrich_raw_csv(raw_path, on_progress=None) -> dict

`on_progress(done, total, message)` is called as rows complete so a server can
stream status to the UI.

Can also be run from the CLI:
    python3 omnibase_pipeline.py data/raw_uploads/somefile.csv
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Reuse OmniBase's proven cleansing helpers — single source of truth.
import omni_engine as oe
import tech_fingerprint as tf

load_dotenv(Path(__file__).parent / ".env")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR      = Path(__file__).parent
DATA_DIR      = BASE_DIR / "data"
OUT_COMPANIES = DATA_DIR / "companies.csv"
OUT_CONTACTS  = DATA_DIR / "contacts.csv"
ENRICHED_LOG  = DATA_DIR / "enriched_leads.csv"

API_KEY       = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL_SLUG    = "deepseek/deepseek-chat"   # standardized on DeepSeek (decision §8.3)

CONCURRENT_LIMIT  = 5
MAX_CONTENT_CHARS = 5000
RECORD_ID_LEN     = 11

ENRICHED_FIELDS = [
    "business_name", "url", "phone", "email", "raw_address",
    "city", "state", "zip_code", "sub_industry", "commercial_residential",
    "summary", "google_rating", "google_review_count",
    "tech_stack", "marketing_tools", "scraped_at",
]

SYSTEM_PROMPT = """
You are a precise data extraction assistant. Analyze this local home service business website text.
Output exactly this JSON format. If a piece of data is not found, use "N/A". Do not include extra text.

{
    "business_name": "Name of the business",
    "phone": "Phone number",
    "email": "Email address",
    "raw_address": "Full physical address (e.g., 123 Main St, Long Beach, CA 90802)",
    "sub_industry": "Specific niche (e.g., Maid Service, Carpet Cleaning, Window Washing)",
    "commercial_residential": "Commercial, Residential, or Commercial/Residential",
    "summary": "A sharp, conversion-focused 1-sentence description of what they do."
}
"""

# ---------------------------------------------------------------------------
# Deterministic Record ID  (decision: derive from domain, stable across runs)
# ---------------------------------------------------------------------------

def domain_of(url: str) -> str:
    """Extract a normalized bare domain from a url (no scheme, no www, no path)."""
    if not url:
        return ""
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.split("/")[0].split("?")[0].strip()
    return u


def record_id_for(url: str, fallback: str = "") -> str:
    """
    Deterministic 11-digit TEXT Record ID derived from the company domain.
    Same domain → same ID on every run (enables update-in-place dedupe).
    Falls back to business name if no domain. Always 11 digits, leading zeros ok.
    """
    seed = domain_of(url) or (fallback or "").strip().lower()
    if not seed:
        seed = "unknown-" + datetime.now(timezone.utc).isoformat()
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
    # Take a slice of the hex → int → mod into 11-digit space, zero-pad.
    num = int(digest[:15], 16) % (10 ** RECORD_ID_LEN)
    return str(num).zfill(RECORD_ID_LEN)


# ---------------------------------------------------------------------------
# Address → city / state / zip
# ---------------------------------------------------------------------------

_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_STATE_IN_TAIL_RE = re.compile(r"\b([A-Za-z]{2})\b")


def parse_address(address_str: str) -> tuple[str, str, str]:
    """
    Parse a combined address → (city, state, zip_code).
    Returns blanks for anything unparseable (caller flags row Pending).
    No hardcoded city/state defaults — honest blanks instead.
    """
    if not address_str or str(address_str).strip().upper() in ("", "N/A", "NULL", "NONE"):
        return "", "", ""

    s = str(address_str).strip()
    zip_code = ""
    zm = _ZIP_RE.search(s)
    if zm:
        zip_code = zm.group(1)

    parts = [p.strip() for p in s.split(",") if p.strip()]
    city, state = "", ""
    if len(parts) >= 2:
        # Last part usually "ST 90802" or "ST"; second-to-last is city.
        tail = parts[-1]
        sm = _STATE_IN_TAIL_RE.search(tail)
        if sm:
            state = sm.group(1).upper()
        city = parts[-2]
        # If second-to-last looks like it IS the state (rare), shift.
        if len(parts) >= 3 and len(city) == 2 and city.isalpha():
            city = parts[-3]
    elif len(parts) == 1:
        # Single chunk — try to lift a trailing state token.
        sm = _STATE_IN_TAIL_RE.search(parts[0])
        if sm:
            state = sm.group(1).upper()

    # Run through omni_engine's normalizer for title-case + 2-letter cleanup.
    city, state = oe.split_city_state(city, state, default_state="")
    if state == "" and not city:
        return "", "", zip_code
    return city, state, zip_code


# ---------------------------------------------------------------------------
# Raw input reading + column detection
# ---------------------------------------------------------------------------

def read_raw_rows(path: Path) -> list[dict]:
    with open(path, mode="r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def detect_columns(fieldnames: list[str]) -> dict:
    """Find url / rating / review-count / name columns by fuzzy header match."""
    cols = {"url": None, "rating": None, "reviews": None, "name": None}
    for col in fieldnames:
        lc = col.lower().strip()
        if cols["url"] is None and ("website_url" in lc or "website" in lc or lc == "url"):
            cols["url"] = col
        if cols["rating"] is None and "rating" in lc:
            cols["rating"] = col
        if cols["reviews"] is None and ("review_count" in lc or "review count" in lc or "reviews" in lc):
            cols["reviews"] = col
        if cols["name"] is None and ("company" in lc or "business" in lc or lc == "name"):
            cols["name"] = col
    return cols


def clean_url(raw: str) -> str:
    if not raw or not isinstance(raw, str):
        return ""
    u = raw.strip()
    if not u or u.lower() in ("nan", "null", "none", "n/a", "⚠️ no url provided"):
        return ""
    # Skip Google redirect/aclk junk that sometimes lands in the url column.
    if "google.com/aclk" in u or "/maps/place/" in u:
        return ""
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


def strip_image_email(email: str) -> str:
    """Drop false-positive emails that are really image filenames."""
    if not email or email.upper() == "N/A":
        return ""
    if re.search(r"\.(png|jpg|jpeg|gif|svg|webp)$", email.strip().lower()):
        return ""
    return email.strip().lower()


# ---------------------------------------------------------------------------
# DeepSeek extraction (sync; called in executor)
# ---------------------------------------------------------------------------

def extract_lead_from_text(content: str) -> dict:
    if not API_KEY:
        raise EnvironmentError("OPENROUTER_API_KEY is missing from .env")
    payload = json.dumps({
        "model": MODEL_SLUG,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": content},
        ],
        "temperature": 0.1,
    }).encode("utf-8")
    req = urllib.request.Request(
        OPENROUTER_URL, data=payload,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:8000",
            "X-Title": "OmniBase Pipeline",
        },
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        raw_text = data["choices"][0]["message"]["content"]
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Per-row worker
# ---------------------------------------------------------------------------

async def _process_row(raw_row, cols, semaphore, crawler, results, idx):
    """Crawl + enrich a single raw row. Stores an enriched dict in results[idx]."""
    name = (raw_row.get(cols["name"]) or "").strip() if cols["name"] else ""
    url = clean_url(raw_row.get(cols["url"], "")) if cols["url"] else ""
    rating = (raw_row.get(cols["rating"]) or "").strip() if cols["rating"] else ""
    reviews = (raw_row.get(cols["reviews"]) or "").strip() if cols["reviews"] else ""

    ai_data, source = {}, ""
    if url:
        async with semaphore:
            try:
                result = await crawler.arun(url=url)
                source = result.html or ""
                content = (result.markdown or "")[:MAX_CONTENT_CHARS]
            except Exception as e:
                content, source = "", ""
            if content.strip():
                try:
                    loop = asyncio.get_running_loop()
                    ai_data = await loop.run_in_executor(None, extract_lead_from_text, content)
                except Exception:
                    ai_data = {}

    fp = tf.fingerprint(source)
    city, state, zip_code = parse_address(ai_data.get("raw_address", ""))

    enriched = {
        "business_name": ai_data.get("business_name") if ai_data.get("business_name") not in (None, "N/A") else name,
        "url": url,
        "phone": ai_data.get("phone", "") if ai_data.get("phone") != "N/A" else "",
        "email": strip_image_email(ai_data.get("email", "")),
        "raw_address": ai_data.get("raw_address", ""),
        "city": city,
        "state": state,
        "zip_code": zip_code,
        "sub_industry": ai_data.get("sub_industry", "") if ai_data.get("sub_industry") != "N/A" else "",
        "commercial_residential": ai_data.get("commercial_residential", "") if ai_data.get("commercial_residential") != "N/A" else "",
        "summary": ai_data.get("summary", "") if ai_data.get("summary") != "N/A" else "",
        "google_rating": rating,
        "google_review_count": reviews,
        "tech_stack": fp["tech_stack"],
        "marketing_tools": fp["marketing_tools"],
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
    results[idx] = enriched


# ---------------------------------------------------------------------------
# Enriched → companies.csv (with update-in-place by Record ID)
# ---------------------------------------------------------------------------

def macro_industry_for(text: str) -> tuple[str, str]:
    """Use omni_engine's taxonomy to classify macro + sub industry from text."""
    blob = (text or "").lower()
    for macro, subs in oe.TAXONOMY.items():
        for sub, kws in subs.items():
            if any(k in blob for k in kws):
                return macro, sub
    return "", ""


def load_existing_companies() -> dict[str, dict]:
    """Load companies.csv keyed by record_id for update-in-place."""
    existing: dict[str, dict] = {}
    if OUT_COMPANIES.exists() and OUT_COMPANIES.stat().st_size > 0:
        with open(OUT_COMPANIES, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                rid = (row.get("record_id") or "").strip()
                if rid:
                    existing[rid] = row
    return existing


def enriched_to_company(e: dict) -> dict:
    """Map one enriched row → a companies.csv record dict."""
    rid = record_id_for(e.get("url", ""), e.get("business_name", ""))
    classify_text = " ".join([
        e.get("business_name", ""), e.get("sub_industry", ""), e.get("summary", "")
    ])
    macro, sub = macro_industry_for(classify_text)
    sub = e.get("sub_industry") or sub

    pending = not (e.get("city") and e.get("state"))
    return {
        "record_id": rid,
        "company_name": (e.get("business_name") or "").title(),
        "website": e.get("url", ""),
        "has_website": "Yes" if e.get("url") else "No",
        "market_segment": "",
        "macro_industry": macro,
        "sub_industries": sub,
        "business_sector": e.get("commercial_residential", ""),
        "city": e.get("city", ""),
        "state": e.get("state", ""),
        "zip_code": e.get("zip_code", ""),
        "employee_count": "",
        "annual_revenue": "",
        "website_traffic": "",
        "google_rating": e.get("google_rating", ""),
        "google_review_count": e.get("google_review_count", ""),
        "tech_stack": e.get("tech_stack", ""),
        "marketing_tools": e.get("marketing_tools", ""),
        "owner_name": "",
        "owner_title": "",
        "owner_email": e.get("email", ""),
        "owner_phone": re.sub(r"\D", "", e.get("phone", "")),
        "status": "Pending" if pending else "Pending",
        "date_imported": date.today().isoformat(),
    }


def merge_company(old: dict, new: dict) -> dict:
    """Update-in-place: keep old values, fill blanks / refresh from new."""
    merged = dict(old)
    for k, v in new.items():
        if v not in (None, "") :
            # Don't overwrite a non-empty old value with a blank; otherwise refresh.
            merged[k] = v
        elif k not in merged:
            merged[k] = v
    merged["date_imported"] = old.get("date_imported") or new["date_imported"]
    return merged


def write_companies(records: list[dict]) -> None:
    """Write companies.csv with QUOTE_NONNUMERIC so record_id/zip stay TEXT."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_COMPANIES, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=oe.COMPANY_COLUMNS, quoting=csv.QUOTE_NONNUMERIC)
        w.writeheader()
        for r in records:
            w.writerow({c: r.get(c, "") for c in oe.COMPANY_COLUMNS})


def ensure_contacts_stub() -> None:
    """Create an empty contacts.csv with the right header if missing."""
    if not OUT_CONTACTS.exists() or OUT_CONTACTS.stat().st_size == 0:
        with open(OUT_CONTACTS, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=oe.CONTACT_COLUMNS, quoting=csv.QUOTE_NONNUMERIC)
            w.writeheader()


def append_enriched_log(rows: list[dict]) -> None:
    """Append the raw enriched rows to enriched_leads.csv as an audit trail."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_header = not ENRICHED_LOG.exists() or ENRICHED_LOG.stat().st_size == 0
    with open(ENRICHED_LOG, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ENRICHED_FIELDS)
        if write_header:
            w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in ENRICHED_FIELDS})


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def _run(raw_path: Path, on_progress=None) -> dict:
    raw_rows = read_raw_rows(raw_path)
    if not raw_rows:
        return {"companies": 0, "contacts": 0, "message": "No rows in input."}
    cols = detect_columns(list(raw_rows[0].keys()))
    total = len(raw_rows)
    results: list[dict | None] = [None] * total

    def progress(done, msg=""):
        if on_progress:
            on_progress(done, total, msg)

    progress(0, "Starting crawl…")
    semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)

    # Lazy import so the module loads even if crawl4ai isn't installed yet.
    from crawl4ai import AsyncWebCrawler

    done = 0
    async with AsyncWebCrawler() as crawler:
        # Process in small batches so progress updates stream.
        batch = 10
        for start in range(0, total, batch):
            chunk = list(range(start, min(start + batch, total)))
            tasks = [
                _process_row(raw_rows[i], cols, semaphore, crawler, results, i)
                for i in chunk
            ]
            await asyncio.gather(*tasks)
            done += len(chunk)
            progress(done, f"Enriched {done}/{total}")

    enriched = [r for r in results if r]
    append_enriched_log(enriched)

    # Cleanse + map → companies, update-in-place by Record ID.
    existing = load_existing_companies()
    for e in enriched:
        comp = enriched_to_company(e)
        rid = comp["record_id"]
        existing[rid] = merge_company(existing[rid], comp) if rid in existing else comp

    write_companies(list(existing.values()))
    ensure_contacts_stub()

    progress(total, "Done")
    return {
        "companies": len(existing),
        "new_or_updated": len(enriched),
        "contacts": 0,
        "companies_path": str(OUT_COMPANIES),
        "message": f"Enriched {len(enriched)} rows → {len(existing)} total companies.",
    }


def enrich_raw_csv(raw_path, on_progress=None) -> dict:
    """Public sync entry point. Runs the full async pipeline to completion."""
    return asyncio.run(_run(Path(raw_path), on_progress))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 omnibase_pipeline.py <raw_csv_path>")
        sys.exit(1)

    def _cli_progress(done, total, msg):
        pct = int(done / total * 100) if total else 0
        print(f"  [{pct:3d}%] {msg}")

    summary = enrich_raw_csv(sys.argv[1], _cli_progress)
    print("\n" + json.dumps(summary, indent=2))
