# OmniBase Market Intelligence Platform — Technical Spec

> Frozen design state / build handoff. Date: 2026-06-22. Supersedes "Lead Manager Pro".
> Baseline artifact: `lead_manager.html` (Desktop). Read this file first to restore context in a new session.

---

## 0. Baseline (current implementation — what already exists)
- Single self-contained file `lead_manager.html` (~1,474 lines). Vanilla HTML5 + CSS3 + ES2020+. Zero external deps, fully offline (no CDN/`fetch`). Double-click to run.
- Storage: **IndexedDB** db `lead_manager_pro`, object store `leads` (keyPath `id`). Side data in `localStorage`: `lm_settings`, `lm_presets`, `lm_column_map`, `lm_visible_columns`, `lm_theme`.
- Features live now: virtual-scroll table (recycles ~24 DOM rows @ any N); CSV import wizard (3-step, inline Blob **Web Worker** parser, column-mapping w/ memory + custom-field detection, dedupe-by-email, append/replace); CSV export (all/filtered/selected); advanced filters + saved presets; bulk Export/Delete; settings (row height, date format, delimiter, JSON backup/restore, clear-all DELETE gate); dark mode; single-level undo + toasts; keyboard shortcuts; notes slide-over.
- Current model = **single flat `leads` table**, already de-pipelined. Columns: Company · Contact Name · Email · Phone · Industry · City · State · Date Imported · Actions. Record fields: `id, company, contact_name, first_name, last_name, email, phone, website, industry, city, state, country, source, notes[], custom_fields{}, created_at, updated_at`.
- Key functions to reuse/extend: `parseCSV` (worker), `rowsToLeads()`, `buildMapping()`, `DB.*` wrapper, `renderTable()`/virtual scroll, `applyFilters()`, `doExport()`/`exportColumns()`, `setView()`.

---

## 1. Core Rebrand
- New name: **"OmniBase Market Intelligence Platform"** — replace ALL legacy "Lead Manager Pro" branding in `lead_manager.html`.
- Touch points: `<title>`, sidebar `.logo` text + mark ("L"→"O"), help/about copy, export filename prefix (`leads_export_`→`omnibase_export_`), any "lead"-centric labels in headings.
- Positioning shift: lead CRM → **market-intelligence / data product** (B2B firmographic database, ZoomInfo/Apollo-style). Keep offline-first, single-file ethos.
- Optional: bump IndexedDB db name → `omnibase` (NOTE: renaming the DB orphans existing data — provide one-time migration from `lead_manager_pro` or keep old name).

## 2. Database Model — relational dual-dataset
- Move from one flat table → **two related datasets**:
  - **`companies.csv`** (primary) — Firmographics + Tech Stack + Owner Contact (owner embedded on the company record).
  - **`contacts.csv`** (secondary) — secondary stakeholders linked to a company.
- IndexedDB: two stores `companies` + `contacts` (each keyPath `record_id`). Import wizard gains a **target-dataset selector** (Companies vs Contacts).
- Field groups:
  - companies: `record_id`, company name, website/domain, **market_segment** (SMB/Mid-Market/Enterprise), **industry** (HVAC/Plumbing/Electrical), city, state, employee_count, revenue, **tech_stack** (multi/array), owner_name, owner_title, owner_email, owner_phone, status, date_imported, custom_fields{}.
  - contacts: `record_id`, **company_record_id** (FK → companies.record_id), name, title, email, phone, status, date_imported.
- Relationship: contacts join to companies via `company_record_id`. *(Inferred join glue — confirm: do contacts carry their own Record ID AND a separate company FK, or share the company's Record ID?)*

## 3. Data Integrity Rule — Record ID
- `Record ID` = **Column A** in BOTH `companies.csv` and `contacts.csv`.
- Exactly **11 digits**, preserve leading zeros.
- Handle strictly as a **TEXT STRING** end-to-end — never numeric. Prevents Excel/Sheets **scientific-notation corruption** (e.g. `1.23457E+10`) and leading-zero loss.
- Enforcement: parser emits Record ID quoted/as text; importer maps Column A → text field (no numeric coercion, no `parseInt`); exporter writes it back as text (quote if delimiter/leading-zero risk). Treat as the primary key (dedupe + join on it).

## 4. Data Cleansing Logic — automated Python parser
- Standalone Python preprocessor (run before import) that ingests raw export(s) and emits clean `companies.csv` + `contacts.csv`.
- Rules:
  - **Isolate Owners**: detect owner/principal role → route owner to the company record's Owner Contact fields; route all other people → `contacts.csv` as secondary stakeholders.
  - **Split mixed City/State**: parse combined strings (e.g. `"Austin, TX"`, `"Austin TX"`) → separate clean `city` + 2-letter `state` columns.
  - **Default status** = **`Pending`** on every new record.
  - Record ID: ensure 11-digit text (zero-pad / strip non-digits / reject malformed); emit as string.
  - General hygiene: trim, Title Case company/city, lowercase email, digits-only phone, normalize state to 2-letter, dedupe on Record ID.
- Output: UTF-8 CSV, Record ID Column A, headers matching import mapping. *(Deliverable: `clean_omnibase.py` — to be written.)*

## 5. UI Enhancements
- **Tabbed navigation**: top-level tabs **Companies View** | **Contacts View** — each renders its own dataset/table (reuse virtual scroll; column sets differ per tab).
- Interactive filter controls (chips/dropdowns) on each view:
  - **Market Segment**: SMB · Mid-Market · Enterprise.
  - **Industry**: HVAC · Plumbing · Electrical.
  - **Status**: (incl. `Pending` default; e.g. Pending · Verified · Contacted · Archived — confirm full set).
- Wire filters into existing `applyFilters()`; add Market Segment + Status as first-class fields (not just custom). Companies View shows firmographics/tech-stack/owner cols; Contacts View shows stakeholder cols + parent company.

## 6. Hot Keys Required
- **Hot Key 1 — Company cleanup + URL verify**: Title-Case the company name AND verify/auto-fill website via **Clearbit Autocomplete (free)**.
  - Endpoint: `https://autocomplete.clearbit.com/v1/companies/suggest?query=<name>` → returns `{name, domain, logo}`; fill `website`/domain from top match.
  - ⚠️ Requires ONE outbound network call → **breaks the offline-only constraint**; gate behind explicit user action + graceful offline fallback. Clearbit is now HubSpot-owned — verify endpoint still free/available; have a fallback (skip + manual).
- **Hot Key 2 — Industry auto-populate**: keyword scan of company name/website/text → set Industry.
  - Keyword map: HVAC ← {hvac, heating, cooling, air, a/c, furnace, refrigeration}; Plumbing ← {plumb, plumbing, pipe, drain, rooter, sewer}; Electrical ← {electric, electrical, electrician, wiring, voltage}. Fully offline.
- Binding: expose as toolbar buttons + key shortcuts; operate on selected row(s) or current dataset (batch). Register in existing keyboard handler + help modal. *(Exact keys TBD — suggest a dedicated "Enrich" action menu to avoid OS clashes.)*

---

## 7. Constraints & open questions (resolve before/while building)
- Offline tension: Hot Key 1 (Clearbit) is the only feature needing the network — keep everything else offline; isolate the fetch.
- Record ID semantics for contacts: own ID + company FK vs shared company ID (see §2).
- Status vocabulary: full allowed set beyond `Pending` (see §5).
- DB rename/migration path if `lead_manager_pro` → `omnibase` (see §1).
- Relational export: round-trip must preserve Record ID as text and the company↔contact link.

## 8. Build order (suggested)
1. Rebrand strings (§1). 2. Split storage into `companies`/`contacts` stores + migrate (§2). 3. Record-ID text handling across import/export (§3). 4. `clean_omnibase.py` parser (§4). 5. Tabbed UI + Segment/Industry/Status filters (§5). 6. Hot Key 2 (offline) then Hot Key 1 (network-gated) (§6).
