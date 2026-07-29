"""
server.py — OmniBase local backend (FastAPI)

Serves the OmniBase UI (lead_manager.html) and powers the in-app enrichment:
upload a raw scrape CSV → crawl + DeepSeek + fingerprint → companies.csv → UI.

Run:
    python3 server.py          # starts on http://127.0.0.1:8000 and opens browser
    (or use the OmniBase.command launcher)

Endpoints:
    GET  /                 → serves lead_manager.html
    POST /enrich           → upload raw CSV, kicks off background enrichment job
    GET  /progress/{job}   → job status { done, total, message, state }
    GET  /companies        → current companies.csv as JSON
    GET  /contacts         → current contacts.csv as JSON
    GET  /health           → { ok, api_key_present }
"""

from __future__ import annotations

import csv
import io
import threading
import uuid
import webbrowser
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse

import omnibase_pipeline as pipeline

BASE_DIR  = Path(__file__).parent
DATA_DIR  = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "raw_uploads"
HTML_FILE = BASE_DIR / "lead_manager.html"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="OmniBase Local Backend")

# In-memory job registry: job_id → progress dict
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def _set_progress(job_id: str, done: int, total: int, message: str, state: str = "running"):
    with JOBS_LOCK:
        JOBS[job_id] = {"done": done, "total": total, "message": message, "state": state}


def _run_job(job_id: str, raw_path: Path):
    """Background thread: run the (sync) pipeline, stream progress into JOBS."""
    def on_progress(done, total, message):
        _set_progress(job_id, done, total, message, "running")
    try:
        summary = pipeline.enrich_raw_csv(raw_path, on_progress)
        with JOBS_LOCK:
            JOBS[job_id] = {
                "done": JOBS.get(job_id, {}).get("total", 0),
                "total": JOBS.get(job_id, {}).get("total", 0),
                "message": summary.get("message", "Done"),
                "state": "done",
                "summary": summary,
            }
    except Exception as e:  # noqa: BLE001
        _set_progress(job_id, 0, 0, f"Error: {e}", "error")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index():
    if HTML_FILE.exists():
        return HTMLResponse(HTML_FILE.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>OmniBase</h1><p>lead_manager.html not found.</p>", status_code=404)


@app.get("/health")
def health():
    return {"ok": True, "api_key_present": bool(pipeline.API_KEY)}


@app.post("/enrich")
async def enrich(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Please upload a .csv file.")
    contents = await file.read()
    safe_name = f"{uuid.uuid4().hex[:8]}_{Path(file.filename).name}"
    dest = UPLOAD_DIR / safe_name
    dest.write_bytes(contents)

    # Pre-count rows so the UI has a total immediately.
    try:
        total = max(0, sum(1 for _ in io.StringIO(contents.decode("utf-8-sig"))) - 1)
    except Exception:
        total = 0

    job_id = uuid.uuid4().hex
    _set_progress(job_id, 0, total, "Queued", "running")
    threading.Thread(target=_run_job, args=(job_id, dest), daemon=True).start()
    return {"job_id": job_id, "total": total}


@app.get("/progress/{job_id}")
def progress(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job id")
    return job


def _csv_to_json(path: Path):
    if not path.exists() or path.stat().st_size == 0:
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


@app.get("/companies")
def companies():
    return JSONResponse(_csv_to_json(DATA_DIR / "companies.csv"))


@app.get("/contacts")
def contacts():
    return JSONResponse(_csv_to_json(DATA_DIR / "contacts.csv"))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def _open_browser():
    import time
    time.sleep(1.5)
    webbrowser.open("http://127.0.0.1:8000/")


if __name__ == "__main__":
    import uvicorn
    threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
