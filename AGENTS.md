# AGENTS.md — working in this repository

Guidance for AI agents and human contributors. Read this before changing code.

## What this project is

**Servey OCR** turns photos of a handwritten K-Electric meter-survey notebook
into an `.xlsx` that is *byte-compatible* with the reference workbook
`Samples or examle data/SACHAL SURVEY - IMAGES DATA.xlsx`. ("Sachal" is the name
of the surveyed area and appears only in that source-data filename — the app
itself is **Servey OCR**.)

The prime directive is **no silent errors**. Every value is either verified
correct or explicitly flagged for a human. A change that could make the pipeline
emit a plausible-but-wrong value without flagging it is a bug, even if tests
pass.

## Architecture

Two layers, kept strictly separate:

1. **`local_ocr/` — the deterministic core.** No cloud, no UI, no I/O beyond
   files. This is the source of truth and is covered by 154 tests. Pipeline:

   ```
   RowRecord ─▶ correction ─▶ assembly ─▶ audit ─▶ OutputRow ─▶ xlsx
   ```

   - `models.py` — `Cell`, `RowRecord`, `OutputRow`, `Status`, `BatchAudit`.
   - `correction/pipeline.py` — `Corrector.correct_page()` runs every rule in a
     fixed order (section markers → ditto → strike-out → grammar/whitelist).
   - `assembly/batch.py` — `assemble()` orders pages by survey range, heals
     groups that straddle a page break, binds remarks, runs audits.
   - `export/xlsx_exporter.py` — `write_workbook()`; reproduces exact cell types.

2. **`app/` — the desktop app.** A thin FastAPI layer that *drives* the core.
   Two recognition modes, same downstream flow
   (`upload → recognise → correct → review → export`):
   - `recognizer.py` — **cloud** vision via REST → raw rows. Gemini and Grok
     share the prompt, JSON parsing and error handling; each provider is only a
     request builder + response extractor in the `_PROVIDERS` table. Add a
     provider there, not by copying the module.
   - `local_engine.py` — **Local OCR** (offline): `local_ocr.layout.ruled`
     segments the page, `local_ocr.ocr` engine (TrOCR-base) reads each cell.
     Decoding is grammar-constrained (`local_ocr/ocr/constrained.py`).
   - `pipeline.py` — `process()` (correct + assemble → review grid + audit; also
     flags low-confidence cells) and `export()` (grid → workbook, verbatim). The
     **only** place the app reaches into `local_ocr`.
   - `server.py` — JSON API (`/api/recognize`, `/api/recognize_local`,
     `/api/validate`, `/api/export`) + serves `static/` (vanilla-JS SPA).
   - `settings_store.py` — persists the Gemini key/model, local engine, and mode.

**Rule: the app never re-implements core logic.** New correction or assembly
behaviour goes in `local_ocr/` with tests, not in `app/`.

## Segmentation and constrained decoding are measured, not guessed

`scripts/eval_segmentation.py` scores the segmenters against the manifest's known
row counts and page sides. Change a threshold in `local_ocr/layout/ruled.py` only
alongside a run of that script, and **look at the crops** (render them; blank or
half-cut crops are invisible in an accuracy number and were the actual bug that
made local OCR unusable). Constants there were fitted at 960x1280 and need a
re-check on full-resolution originals.

## The default recognizer is cloud — do not propose a local VLM

This machine (8 GB RAM, integrated GPU) cannot run a 3B vision model — it
reserves ~10 GB at runtime and thrashes. The Ollama/qwen experiment was removed
deliberately. If fully-offline recognition is ever needed, the path is a small
local OCR engine (TrOCR/Paddle adapters still in `local_ocr/ocr/`), **not**
another VLM. See `MEMORY.md`.

## Conventions

- **Python 3.11+**, `from __future__ import annotations`, type hints, dataclasses.
- Comments explain *why*, matching the existing dense-but-purposeful docstrings.
  Match the surrounding style; don't add narration.
- No new heavy deps in the app path (no torch/opencv/pandas pulled by `app/` —
  it keeps the `.exe` at ~45 MB). The core may use rapidfuzz/openpyxl/yaml.
- **Frozen-aware paths** live in `local_ocr/paths.py`: read-only resources from
  `BUNDLE_ROOT`, writable data (`output/`, `app_settings.json`) from `DATA_ROOT`.
  Never hardcode paths that break under PyInstaller.

## Running & building

```bash
python -m pip install -r requirements.txt
python run_app.py                                  # dev server, opens browser
python -m pytest tests                             # 154 tests — keep them green
python -m PyInstaller ServeyOCR.spec --noconfirm   # -> dist/ServeyOCR.exe
```

## Before you commit

- `python -m pytest tests` passes (all 154).
- No secrets committed. `app_settings.json` holds the Gemini API key and is
  git-ignored — **never** add it, and never paste a key into code or docs.
- `output/`, `dist/`, `build/`, `__pycache__/` stay out of git (see `.gitignore`).
- If you added a correction/assembly rule, it has a test in `tests/unit/` citing
  the real page + workbook row that proves the expected value, and the golden
  round-trip (`tests/integration/test_export_golden.py`) still passes.
