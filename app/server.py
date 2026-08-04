"""FastAPI server for the OCR desktop app.

Serves the single-page UI and a small JSON API over the pipeline. State is
minimal: uploaded images live on disk under ``output/_uploads/<job>/`` and a
process-local registry maps a job id to its images. Everything else -- the grid,
the edits -- lives in the browser and is posted back when needed, so a refresh
never corrupts a run and there is no database to manage.
"""

from __future__ import annotations

import shutil
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import local_engine, pipeline, recognizer, settings_store
from local_ocr import paths

STATIC_DIR = paths.STATIC_DIR
OUTPUT_DIR = paths.OUTPUT_DIR
UPLOAD_ROOT = OUTPUT_DIR / "_uploads"
ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}

app = FastAPI(title="Servey OCR", version="1.0.0")

#: job_id -> {image_name: absolute_path}, in insertion (upload) order.
_JOBS: dict[str, dict[str, Path]] = {}


# --------------------------------------------------------------------- models


class SettingsIn(BaseModel):
    gemini_api_key: str | None = None
    gemini_model: str | None = None
    grok_api_key: str | None = None
    grok_model: str | None = None
    local_engine: str | None = None
    mode: str | None = None


class TestKeyIn(BaseModel):
    provider: str | None = None
    api_key: str | None = None
    model: str | None = None


class RecognizeIn(BaseModel):
    job: str
    images: list[str] | None = None  # subset of names; None = all in the job
    #: Cloud provider to use; defaults to whatever the saved mode selects.
    provider: str | None = None


class GridRow(BaseModel):
    page: str = "manual"
    index: int = 0
    survey: str = ""
    meter: str = ""
    remarks: str = ""


class ValidateIn(BaseModel):
    rows: list[GridRow]


class ExportIn(BaseModel):
    rows: list[GridRow]
    filename: str | None = None


# ------------------------------------------------------------------- helpers


def _pages_from_rows(rows: list[GridRow]) -> list[tuple[str, list[dict]]]:
    """Group flat grid rows back into per-page row lists, preserving order."""
    by_page: dict[str, list[GridRow]] = defaultdict(list)
    order: list[str] = []
    for row in rows:
        page = row.page or "manual"
        if page not in by_page:
            order.append(page)
        by_page[page].append(row)

    pages: list[tuple[str, list[dict]]] = []
    for page in order:
        ordered = sorted(by_page[page], key=lambda r: r.index)
        pages.append(
            (page, [{"survey": r.survey, "meter": r.meter, "remarks": r.remarks} for r in ordered])
        )
    return pages


# -------------------------------------------------------------------- routes


def _settings_payload(settings) -> dict:
    """Public settings plus whether local OCR can actually run in this build."""
    payload = settings.public()
    ok, reason = local_engine.availability(settings.local_engine)
    payload["local_available"] = ok
    payload["local_unavailable_reason"] = reason
    return payload


@app.get("/api/settings")
def get_settings() -> dict:
    return _settings_payload(settings_store.load())


@app.post("/api/settings")
def post_settings(body: SettingsIn) -> dict:
    updated = settings_store.update(
        gemini_api_key=body.gemini_api_key,
        gemini_model=body.gemini_model,
        grok_api_key=body.grok_api_key,
        grok_model=body.grok_model,
        local_engine=body.local_engine,
        mode=body.mode,
    )
    return _settings_payload(updated)


@app.post("/api/settings/test")
def post_test_key(body: TestKeyIn) -> dict:
    settings = settings_store.load()
    provider = (body.provider or "gemini").strip()
    if provider not in recognizer.PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider {provider!r}.")
    key = (body.api_key or "").strip() or settings.key_for(provider)
    model = (body.model or "").strip() or settings.model_for(provider)
    ok, message = recognizer.test_key(key, model, provider=provider)
    return {"ok": ok, "message": message}


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)) -> dict:
    job = uuid.uuid4().hex[:12]
    job_dir = UPLOAD_ROOT / job
    job_dir.mkdir(parents=True, exist_ok=True)

    registry: dict[str, Path] = {}
    saved: list[dict] = []
    for upload_file in files:
        suffix = Path(upload_file.filename or "").suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            continue
        name = Path(upload_file.filename or "page").name
        # Avoid collisions when two uploads share a filename.
        target = job_dir / name
        counter = 1
        while target.exists():
            target = job_dir / f"{Path(name).stem}_{counter}{suffix}"
            counter += 1
        with target.open("wb") as handle:
            shutil.copyfileobj(upload_file.file, handle)
        registry[target.name] = target
        saved.append(
            {"name": target.name, "url": f"/api/image/{job}/{target.name}"}
        )

    if not saved:
        raise HTTPException(status_code=400, detail="No supported image files were uploaded.")

    _JOBS[job] = registry
    return {"job": job, "images": saved}


@app.get("/api/image/{job}/{name}")
def get_image(job: str, name: str) -> FileResponse:
    registry = _JOBS.get(job)
    if not registry or name not in registry:
        raise HTTPException(status_code=404, detail="Image not found.")
    return FileResponse(registry[name])


@app.post("/api/recognize")
def recognize(body: RecognizeIn) -> JSONResponse:
    registry = _JOBS.get(body.job)
    if not registry:
        raise HTTPException(status_code=404, detail="Upload session not found. Re-upload the images.")

    settings = settings_store.load()
    provider = (body.provider or settings.mode or "gemini").strip()
    if provider not in recognizer.PROVIDERS:
        provider = "gemini"
    label = provider.capitalize()
    if not settings.has_key_for(provider):
        raise HTTPException(
            status_code=400,
            detail=f"No {label} API key configured. Open Settings to add one.",
        )

    api_key = settings.key_for(provider)
    model = settings.model_for(provider)
    names = body.images or list(registry.keys())
    pages: list[tuple[str, list[dict]]] = []
    errors: list[dict] = []
    for name in names:
        path = registry.get(name)
        if path is None:
            continue
        result = recognizer.recognise_page(path, api_key, model, provider=provider)
        if result.error:
            errors.append({"page": result.page, "error": result.error})
            continue
        pages.append(
            (
                result.page,
                [{"survey": r.survey, "meter": r.meter, "remarks": r.remarks} for r in result.rows],
            )
        )

    if not pages and errors:
        # Every page failed -- surface the first reason rather than an empty grid.
        raise HTTPException(status_code=502, detail=errors[0]["error"])

    payload = pipeline.process(pages)
    payload["recognition_errors"] = errors
    return JSONResponse(payload)


@app.post("/api/recognize_local")
def recognize_local(body: RecognizeIn) -> JSONResponse:
    registry = _JOBS.get(body.job)
    if not registry:
        raise HTTPException(status_code=404, detail="Upload session not found. Re-upload the images.")

    settings = settings_store.load()
    available, reason = local_engine.availability(settings.local_engine)
    if not available:
        raise HTTPException(status_code=400, detail=reason)
    try:
        engine = local_engine.get_engine(settings.local_engine)
    except KeyError:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown OCR engine '{settings.local_engine}'. Known: {', '.join(local_engine.known_engines())}.",
        )
    if not engine.available:
        raise HTTPException(
            status_code=400,
            detail=(
                f"The '{settings.local_engine}' engine is not installed "
                f"({engine.unavailable_reason}). Install it in this Python environment, then retry."
            ),
        )

    names = body.images or list(registry.keys())
    pages: list[tuple[str, list[dict]]] = []
    errors: list[dict] = []
    for name in names:
        path = registry.get(name)
        if path is None:
            continue
        result = local_engine.recognise_page_local(path, engine)
        if result.error:
            errors.append({"page": result.page, "error": result.error})
            continue
        pages.append((result.page, result.rows))

    if not pages and errors:
        raise HTTPException(status_code=502, detail=errors[0]["error"])

    payload = pipeline.process(pages)
    payload["recognition_errors"] = errors
    return JSONResponse(payload)


@app.post("/api/validate")
def validate(body: ValidateIn) -> JSONResponse:
    pages = _pages_from_rows(body.rows)
    return JSONResponse(pipeline.process(pages))


@app.post("/api/export")
def export(body: ExportIn) -> dict:
    pages = _pages_from_rows(body.rows)
    processed = pipeline.process(pages)  # normalise + reproduce cell types

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = (body.filename or f"SERVEY_OCR_{stamp}").strip()
    if not name.lower().endswith(".xlsx"):
        name += ".xlsx"
    name = Path(name).name  # no path traversal
    out_path = OUTPUT_DIR / name

    pipeline.export(processed["rows"], out_path, backup_previous=True)
    return {
        "download": f"/api/download/{name}",
        "filename": name,
        "row_count": len(processed["rows"]),
        "audit": processed["audit"],
    }


@app.get("/api/download/{name}")
def download(name: str) -> FileResponse:
    path = OUTPUT_DIR / Path(name).name
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# Mount static assets last so it does not shadow the API routes above.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
