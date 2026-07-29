# OmniBase CRM

A self-hosted, single-file CRM platform built for B2B sales and lead management — inspired by Salesforce's interface and workflow, running entirely in the browser with an optional Python backend for enrichment.

## Features

### Frontend (zero-dependency HTML)
- **Account Records** — full-screen Salesforce-style record pages with editable company details, related contacts, and an activity timeline (Notes, Calls, Emails, Meetings, Tasks)
- **Contact Records** — contact detail pages linked to parent accounts, with their own activity log
- **List Views** — named, saveable filter snapshots (search + segment + industry + status + sort + columns) with a picker bar above the table
- **Virtual scroll table** — handles thousands of rows without pagination; column visibility, multi-sort, and advanced filters
- **New Contact modal** — clean 6-field form: First Name, Last Name, Company (autocomplete), Title, Email, Phone
- **Data Import / Export** — CSV upload with field mapping, CSV export
- **Lead Enrichment** — integrates with the Python backend for tech-stack and contact enrichment
- **SLDS design system** — Salesforce Lightning-inspired navy/blue palette, dark mode toggle

### Backend (FastAPI + Python)
- `server.py` — REST API for enrichment and data persistence
- `omni_engine.py` — enrichment engine (website analysis, tech fingerprinting)
- `tech_fingerprint.py` — detects CRM, CMS, analytics, and ecommerce stack from a domain
- `omnibase_pipeline.py` — batch pipeline for processing lead lists

## Quick Start

### Frontend only (no backend needed)
Open `lead_manager.html` directly in your browser. All data is stored in IndexedDB locally.

### With backend enrichment

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Start the API server
python server.py

# 3. Open lead_manager.html in your browser
open lead_manager.html
```

The frontend auto-detects the local backend at `http://localhost:8000`.

## Data

All CRM data (companies, contacts) is stored in the browser's **IndexedDB** — nothing leaves your machine unless you export it or wire up a remote backend.

The `data/` directory (gitignored) is where the pipeline writes enriched output CSVs. Never commit real company or contact data to version control.

## Stack

| Layer | Tech |
|-------|------|
| Frontend | Vanilla JS, HTML5, CSS (no build step) |
| Storage | IndexedDB (browser-native) |
| Backend | FastAPI + Python 3.11+ |
| Enrichment | httpx, BeautifulSoup, custom fingerprinting |

## License

MIT
