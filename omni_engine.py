"""
omni_engine.py — OmniBase data-cleansing preprocessor
Run before CSV import. Ingests raw_leads.csv, emits clean companies.csv + contacts.csv.

Record IDs are ALWAYS handled as TEXT STRINGS — never numeric.
Prevents Excel/Sheets scientific-notation corruption on 11-digit IDs.

Usage:
    python3 omni_engine.py                        # uses data/raw_leads.csv
    python3 omni_engine.py path/to/raw.csv        # explicit input path
"""

import csv
import json
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date
from pathlib import Path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR       = Path(__file__).parent
DATA_DIR       = BASE_DIR / "data"
RAW_INPUT      = DATA_DIR / "raw_leads.csv"
OUT_COMPANIES  = DATA_DIR / "companies.csv"
OUT_CONTACTS   = DATA_DIR / "contacts.csv"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RECORD_ID_LEN = 11

# Regional default for records where no state can be inferred.
# Override via DEFAULT_STATE env var or pass explicitly.
REGIONAL_DEFAULT_STATE = "FL"

OWNER_TITLE_KEYWORDS = {
    "owner", "principal", "president", "ceo", "founder", "co-founder",
    "proprietor", "managing partner", "managing member",
}

TAXONOMY = {
    "Home Services": {
        "house cleaning": ["cleaning", "maid", "housekeeping", "janitorial"],
        "HVAC":           ["hvac", "heating", "cooling", "air conditioning", "furnace", "refrigeration"],
        "Plumbing":       ["plumb", "plumbing", "pipe", "drain", "rooter", "sewer"],
        "Electrical":     ["electric", "electrical", "electrician", "wiring", "lighting"],
        "Solar":          ["solar", "photovoltaic", "solar panels"],
        "Renovation":     ["renovation", "remodel", "contractor", "kitchen", "bath"],
        "Demolition":     ["demolition", "demo", "wrecking"],
        "Junk Removal":   ["junk", "haul", "waste removal", "trash"],
    },
    "Construction": {
        "General Contracting": ["builder", "construction", "general contractor"],
        "Excavation":          ["excavation", "grading", "dirtwork"],
        "Roofing":             ["roof", "roofing", "shingle"],
    },
    "Facility Maintenance": {
        "Commercial Cleaning": ["janitorial", "office cleaning", "facility service"],
        "Pressure Washing":    ["pressure wash", "power wash"],
        "Window Washing":      ["window wash", "window cleaning"],
    },
}

SECTOR_KEYWORDS = {
    "Commercial":  ["commercial", "facility", "industrial", "b2b", "office", "warehouse"],
    "Residential": ["residential", "home", "house", "consumer"],
}

STATE_LOOKUP = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
    "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

# Matches an inverted "STATE, City" or "STATE City" pattern.
# Group 1 = 2-letter state abbreviation, Group 2 = city name.
# Applied AFTER stripping; input is NOT pre-uppercased so we use [A-Za-z]{2}.
_INVERTED_RE = re.compile(r"^([A-Za-z]{2})[,\s]+(.+)$")

# Matches a standard "City, ST" or "City ST" or "City/ST" pattern.
# Group 1 = city, Group 2 = 2-letter state.
_STANDARD_RE = re.compile(r"^(.+?)\s*[,/\s]\s*([A-Za-z]{2})\s*$")

COMPANY_COLUMNS = [
    "record_id", "company_name", "website", "has_website", "market_segment",
    "macro_industry", "sub_industries", "business_sector",
    "city", "state", "zip_code",
    "employee_count", "annual_revenue", "website_traffic",
    "google_rating", "google_review_count",
    "tech_stack", "marketing_tools",
    "owner_name", "owner_title", "owner_email", "owner_phone",
    "status", "date_imported",
]

CONTACT_COLUMNS = [
    "record_id", "company_record_id", "first_name", "last_name", "title",
    "email", "phone", "status", "date_imported",
]

DEFAULT_STATUS = "Pending"


# ---------------------------------------------------------------------------
# Record ID helpers  (§3)
# ---------------------------------------------------------------------------

def normalize_record_id(value) -> str:
    """
    Coerce any value to a zero-padded 11-digit text string.

    - Strips non-digit characters (handles scientific notation artifacts like
      "1.23457E+10" by stripping the decimal and letter, then re-validating).
    - Raises ValueError if the result is empty or longer than RECORD_ID_LEN digits.
    - Returns a plain Python str — never int, never float.
    """
    raw = str(value).strip()

    # Handle scientific notation (e.g. "1.23457E+10" from Excel corruption).
    # Convert to float first to recover the integer, then re-stringify.
    if re.search(r"[Ee][+\-]?\d+", raw):
        try:
            raw = str(int(float(raw)))
        except (ValueError, OverflowError):
            pass

    digits = re.sub(r"\D", "", raw)

    if not digits:
        raise ValueError(f"Record ID contains no digits: {value!r}")
    if len(digits) > RECORD_ID_LEN:
        raise ValueError(
            f"Record ID too long ({len(digits)} digits, max {RECORD_ID_LEN}): {value!r}"
        )
    return digits.zfill(RECORD_ID_LEN)


# ---------------------------------------------------------------------------
# City / State parser  (§4)
# ---------------------------------------------------------------------------

def split_city_state(
    raw_city: str,
    raw_state: str = "",
    *,
    default_state: str = REGIONAL_DEFAULT_STATE,
) -> tuple[str, str]:
    """
    Normalize messy City and State fields into a clean (city, state) pair.

    Handles four real-world variations found in our raw exports:

    Case 1 — Standard (already split):
        raw_city="Clearwater", raw_state="FL"  →  ("Clearwater", "FL")

    Case 2 — Inverted comma (state first, comma-separated):
        raw_city="FL, Clearwater", raw_state=""  →  ("Clearwater", "FL")

    Case 3 — Inverted space (state first, space-separated):
        raw_city="FL Clearwater", raw_state=""  →  ("Clearwater", "FL")

    Case 4 — Missing state (city only, state unknown):
        raw_city="Clearwater", raw_state=""  →  ("Clearwater", default_state)

    Rules applied to all outputs:
        - Cities → stripped + Title Case
        - States → stripped + 2-letter UPPERCASE
    """
    city  = str(raw_city  or "").strip()
    state = str(raw_state or "").strip()

    # Case 1: both fields already populated and state looks clean.
    if city and len(state) == 2 and state.isalpha():
        return city.title(), state.upper()

    # Cases 2 & 3: detect inverted pattern in the city field.
    # The distinguishing feature: field starts with exactly 2 alpha chars
    # that represent a state abbreviation, followed by comma or whitespace.
    if city:
        m = _INVERTED_RE.match(city)
        if m:
            candidate_state = m.group(1)
            candidate_city  = m.group(2).strip()
            # Heuristic guard: a valid state token is 2 alpha chars only.
            # City names don't start with a bare 2-letter token followed by
            # a comma or significant whitespace — this is safe for US data.
            if len(candidate_state) == 2 and candidate_state.isalpha() and candidate_city:
                return candidate_city.title(), candidate_state.upper()

        # Standard combined format "City, ST" or "City/ST" — fallback scan.
        m2 = _STANDARD_RE.match(city)
        if m2:
            extracted_city  = m2.group(1).strip()
            extracted_state = m2.group(2).strip()
            # Only accept if the state token is 2 alpha chars.
            if len(extracted_state) == 2 and extracted_state.isalpha():
                return extracted_city.title(), extracted_state.upper()

    # Case 4: city present but no state recoverable — apply regional default.
    if city:
        return city.title(), (state.upper() if state else default_state)

    # Both fields empty — return safe empty pair.
    return "", default_state


# ---------------------------------------------------------------------------
# Hygiene pass  (§4)
# ---------------------------------------------------------------------------

def hygiene(record: dict) -> dict:
    """
    General field hygiene — mutates and returns the record dict.

    - Trim all string values
    - Title-case company_name and city
    - Lowercase email
    - Digits-only phone (strips formatting)
    - Normalize state to 2-letter uppercase
    - Split combined city/state fields via split_city_state()
    """
    for key in list(record.keys()):
        if isinstance(record[key], str):
            record[key] = record[key].strip()

    if record.get("company_name"):
        record["company_name"] = record["company_name"].title()

    # Resolve city/state — handles all four messy formats.
    city, state = split_city_state(
        record.get("city", ""),
        record.get("state", ""),
    )
    record["city"]  = city
    record["state"] = state

    state_val = str(record.get("state", "")).strip().lower()
    if state_val:
        if state_val in STATE_LOOKUP:
            record["state"] = STATE_LOOKUP[state_val]
        else:
            alpha_only = re.sub(r"[^A-Za-z]", "", state_val)
            if len(alpha_only) == 2:
                record["state"] = alpha_only.upper()

    if record.get("email"):
        record["email"] = record["email"].strip().lower()

    if record.get("phone"):
        record["phone"] = re.sub(r"\D", "", record["phone"])

    return record


def apply_default_status(record: dict) -> dict:
    """Set status = DEFAULT_STATUS if field is absent or blank."""
    if not record.get("status"):
        record["status"] = DEFAULT_STATUS
    return record


# ---------------------------------------------------------------------------
# Owner isolation + relational router  (§4)
# ---------------------------------------------------------------------------

def _score_owner(title: str) -> int:
    """
    Priority score for selecting the best owner from a group.
    Higher = more likely to be THE primary decision-maker.
    """
    t = title.lower()
    priority = ["owner", "ceo", "president", "principal", "founder",
                "co-founder", "proprietor", "managing partner", "managing member"]
    for i, kw in enumerate(priority):
        if kw in t:
            return len(priority) - i  # higher score = earlier in list
    return 0


def _split_name(full_name: str) -> tuple[str, str]:
    """Split 'First Last' → ('First', 'Last'). Handles single-name gracefully."""
    parts = full_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return (parts[0], "") if parts else ("", "")


def isolate_owners(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Group flat rows by record_id and separate owners from secondary contacts.

    For every unique 11-digit Record ID (kept strictly as a string):
      - Owner row  → embedded onto the company record as owner_* fields.
      - All other rows → contacts.csv, linked via company_record_id FK.

    If no owner is found in a group, the company record is still created with
    blank owner fields (better than losing the company from the dataset).

    Returns:
        company_rows  — list of dicts matching COMPANY_COLUMNS
        contact_rows  — list of dicts matching CONTACT_COLUMNS
    """
    today = date.today().isoformat()

    # ── 1. Group raw rows by record_id ──────────────────────────────────────
    groups: dict[str, list[dict]] = defaultdict(list)
    skipped = 0
    for row in rows:
        try:
            rid = normalize_record_id(row.get("record_id", ""))
        except ValueError as exc:
            print(f"  [SKIP] Bad Record ID — {exc}", file=sys.stderr)
            skipped += 1
            continue
        row["record_id"] = rid  # overwrite with clean value in-place
        groups[rid].append(row)

    if skipped:
        print(f"  [WARN] Skipped {skipped} rows with invalid Record IDs.", file=sys.stderr)

    company_rows: list[dict] = []
    contact_rows: list[dict] = []

    # Contact ID counter — contacts get a generated 11-digit ID since they
    # don't carry their own in the raw flat export.
    contact_counter = 1

    # ── 2. Process each company group ────────────────────────────────────────
    for record_id, group in groups.items():

        # Apply hygiene to every row in the group.
        group = [hygiene(apply_default_status(r)) for r in group]

        # ── 2a. Find the best owner row ─────────────────────────────────────
        owner_candidates = [
            r for r in group if is_owner(r.get("title", ""))
        ]
        # Pick the highest-priority owner title if multiple exist.
        owner_row = (
            max(owner_candidates, key=lambda r: _score_owner(r.get("title", "")))
            if owner_candidates
            else None
        )

        # ── 2b. Build company record ─────────────────────────────────────────
        # Use any row as the source of firmographic data (prefer owner row).
        source = owner_row or group[0]

        owner_first, owner_last = ("", "")
        if owner_row:
            owner_first, owner_last = _split_name(owner_row.get("name", ""))

        _mi, _si, _bs = complex_industry_eval(source.get("company_name", ""))

        company_record = {
            "record_id":       record_id,                                 # TEXT — protected
            "company_name":    source.get("company_name", "").title(),
            "website":         source.get("website", ""),
            "market_segment":  source.get("market_segment", ""),
            "macro_industry":  source.get("macro_industry",  "") or _mi,
            "sub_industries":  source.get("sub_industries",  "") or _si,
            "business_sector": source.get("business_sector", "") or _bs,
            "city":           source.get("city", ""),
            "state":          source.get("state", ""),
            "employee_count": source.get("employee_count", ""),
            "revenue":        source.get("revenue", ""),
            "tech_stack":     source.get("tech_stack", ""),
            "owner_name":     f"{owner_first} {owner_last}".strip() if owner_row else "",
            "owner_title":    owner_row.get("title", "") if owner_row else "",
            "owner_email":    owner_row.get("email", "") if owner_row else "",
            "owner_phone":    owner_row.get("phone", "") if owner_row else "",
            "status":         source.get("status", DEFAULT_STATUS),
            "date_imported":  source.get("date_imported", today),
        }
        company_rows.append(company_record)

        # ── 2c. Route non-owners to contacts ─────────────────────────────────
        for row in group:
            if row is owner_row:
                continue  # owner already embedded on company record

            contact_rid = str(contact_counter).zfill(RECORD_ID_LEN)
            contact_counter += 1

            first, last = _split_name(row.get("name", ""))
            contact_record = {
                "record_id":         contact_rid,   # generated, TEXT
                "company_record_id": record_id,     # FK → companies.record_id, TEXT
                "first_name":        first,
                "last_name":         last,
                "title":             row.get("title", ""),
                "email":             row.get("email", ""),
                "phone":             row.get("phone", ""),
                "status":            row.get("status", DEFAULT_STATUS),
                "date_imported":     row.get("date_imported", today),
            }
            contact_rows.append(contact_record)

    return company_rows, contact_rows


def is_owner(title: str) -> bool:
    """Return True if a contact's title indicates an owner/principal role."""
    return any(kw in title.lower() for kw in OWNER_TITLE_KEYWORDS)


# ---------------------------------------------------------------------------
# Hot Key 2 — Industry auto-populate  (§6, fully offline)
# ---------------------------------------------------------------------------

def complex_industry_eval(text: str) -> tuple[str, str, str]:
    """
    Scans company copy to resolve macro-vertical, sub-trade tag list,
    and target service sector. Operates 100% offline.

    Returns (macro_industry, sub_industries, business_sector).
    """
    lower_text = text.lower()
    matched_macro = None
    matched_subs  = []
    matched_sector = "Residential"

    for macro, sub_dict in TAXONOMY.items():
        for sub_category, keywords in sub_dict.items():
            if any(kw in lower_text for kw in keywords):
                matched_subs.append(sub_category)
                if not matched_macro:
                    matched_macro = macro

    has_comm = any(kw in lower_text for kw in SECTOR_KEYWORDS["Commercial"])
    has_res  = any(kw in lower_text for kw in SECTOR_KEYWORDS["Residential"])
    if has_comm and has_res:
        matched_sector = "Both"
    elif has_comm:
        matched_sector = "Commercial"

    return (
        matched_macro or "Other",
        ", ".join(matched_subs) if matched_subs else "General",
        matched_sector,
    )


# ---------------------------------------------------------------------------
# Hot Key 1 — Company cleanup + URL verify  (§6, NETWORK REQUIRED)
# ---------------------------------------------------------------------------

CLEARBIT_ENDPOINT = "https://autocomplete.clearbit.com/v1/companies/suggest"


def hotkey_company_cleanup(name: str, *, allow_network: bool = False) -> dict:
    """
    Title-Case company name and, if allow_network=True, query Clearbit
    Autocomplete for domain/logo.

    Returns dict: {name, domain, logo}.
    Falls back silently if network is disabled or the call fails.

    ⚠️  Clearbit is now HubSpot-owned — verify endpoint availability before
    relying on it in production. Gate this behind explicit user action.
    """
    clean_name = name.strip().title()
    result: dict = {"name": clean_name, "domain": None, "logo": None}

    if not allow_network:
        return result

    url = f"{CLEARBIT_ENDPOINT}?query={urllib.parse.quote(clean_name)}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
            if data:
                top = data[0]
                result["domain"] = top.get("domain")
                result["logo"]   = top.get("logo")
    except Exception as exc:
        result["_error"] = str(exc)  # silent fallback — caller decides what to show

    return result


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------

def read_csv(path: Path) -> list[dict]:
    """
    Read a CSV into a list of dicts.
    record_id and company_record_id are coerced to str immediately on read
    to prevent any downstream numeric coercion.
    """
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:  # utf-8-sig strips BOM
        reader = csv.DictReader(f)
        for row in reader:
            # Immediately protect ID fields as strings.
            for id_field in ("record_id", "company_record_id"):
                if id_field in row:
                    row[id_field] = str(row[id_field]).strip()
            rows.append(row)
    return rows


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    """
    Write rows to a UTF-8 CSV with QUOTE_NONNUMERIC so every field is quoted.
    This ensures record_id is always emitted as text — Excel/Sheets read it
    as a string and cannot silently convert it to scientific notation.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=columns,
            extrasaction="ignore",
            quoting=csv.QUOTE_NONNUMERIC,  # quotes every field → record_id stays text
        )
        writer.writeheader()
        for row in rows:
            # Belt-and-suspenders: force ID fields to str one final time before write.
            out = dict(row)
            for id_field in ("record_id", "company_record_id"):
                if id_field in out:
                    out[id_field] = str(out[id_field])
            writer.writerow(out)


# ---------------------------------------------------------------------------
# Column mapper — translates raw lead_manager.html export headers → internal
# ---------------------------------------------------------------------------

# Map from common raw header variants → our internal field names.
# Keys are lowercased + stripped for fuzzy matching.
RAW_COLUMN_MAP = {
    "record id":       "record_id",
    "record_id":       "record_id",
    "id":              "record_id",
    "company":         "company_name",
    "company name":    "company_name",
    "company_name":    "company_name",
    "contact name":    "name",
    "contact_name":    "name",
    "name":            "name",
    "full name":       "name",
    "full_name":       "name",
    "first name":      "first_name",
    "first_name":      "first_name",
    "last name":       "last_name",
    "last_name":       "last_name",
    "title":           "title",
    "email":           "email",
    "phone":           "phone",
    "website":         "website",
    "domain":          "website",
    "industry":        "macro_industry",
    "macro_industry":  "macro_industry",
    "sub_industries":  "sub_industries",
    "sub industries":  "sub_industries",
    "business_sector": "business_sector",
    "business sector": "business_sector",
    "sector":          "business_sector",
    "city":            "city",
    "state":           "state",
    "market segment":  "market_segment",
    "market_segment":  "market_segment",
    "segment":         "market_segment",
    "employee count":  "employee_count",
    "employees":       "employee_count",
    "revenue":         "revenue",
    "tech stack":      "tech_stack",
    "status":          "status",
    "date imported":   "date_imported",
    "created_at":      "date_imported",
    "source":          "source",
}


def remap_columns(rows: list[dict]) -> list[dict]:
    """
    Translate raw column headers to internal field names using RAW_COLUMN_MAP.
    Unknown columns are passed through unchanged.
    Handles first_name + last_name → name reconstruction.
    """
    remapped = []
    for row in rows:
        new_row: dict = {}
        for key, val in row.items():
            mapped = RAW_COLUMN_MAP.get(key.lower().strip(), key.lower().strip())
            new_row[mapped] = val

        # If a combined 'name' field came in, split it into first/last.
        if new_row.get("name") and not new_row.get("first_name") and not new_row.get("last_name"):
            first, last = _split_name(new_row.pop("name"))
            new_row["first_name"] = first
            new_row["last_name"]  = last
        # If only first/last were present, ensure 'name' is dropped (contacts use first/last).
        elif new_row.get("first_name") or new_row.get("last_name"):
            new_row.pop("name", None)

        remapped.append(new_row)
    return remapped


# ---------------------------------------------------------------------------
# Main pipeline  (§4)
# ---------------------------------------------------------------------------

def main(input_path: Path | None = None) -> None:
    """
    Full data-cleansing pipeline:

        raw_leads.csv
            │
            ▼
        remap_columns()          — normalize raw headers to internal names
            │
            ▼
        isolate_owners()         — group by record_id, route owners vs contacts
            │                      (hygiene + default_status run inside here)
            ▼
        write_csv()              — companies.csv  (QUOTE_NONNUMERIC → text IDs)
        write_csv()              — contacts.csv   (QUOTE_NONNUMERIC → text IDs)
    """
    src = Path(input_path) if input_path else RAW_INPUT

    if not src.exists():
        print(f"[ERROR] Input file not found: {src}", file=sys.stderr)
        print(
            f"        Place your raw export at '{RAW_INPUT}' or pass a path as an argument.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[OmniBase] Reading: {src}")
    raw_rows = read_csv(src)
    print(f"           {len(raw_rows)} rows loaded.")

    # Translate raw column names to internal schema.
    rows = remap_columns(raw_rows)

    # Relational isolation: owner → company record, others → contacts.
    print("[OmniBase] Isolating owners and routing contacts…")
    company_rows, contact_rows = isolate_owners(rows)

    print(f"           {len(company_rows)} companies  →  {OUT_COMPANIES}")
    print(f"           {len(contact_rows)} contacts   →  {OUT_CONTACTS}")

    # Write outputs — QUOTE_NONNUMERIC ensures record_id is always quoted text.
    write_csv(OUT_COMPANIES, COMPANY_COLUMNS, company_rows)
    write_csv(OUT_CONTACTS,  CONTACT_COLUMNS, contact_rows)

    print("[OmniBase] Done. Import companies.csv then contacts.csv into OmniBase.")


if __name__ == "__main__":
    input_file = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    main(input_file)
