# LOCAL Survey OCR System — Complete Implementation Plan

**Project:** Local, offline handwritten notebook OCR → structured Excel
**Target platform:** Windows (CPU-only)
**Version:** 1.0
**Date:** July 2026

---

## Table of contents

1. Problem definition
2. Data schema
3. System architecture
4. Tech stack
5. Domain rules and algorithms
6. Project folder structure
7. Phase-by-phase build plan
8. Configuration file schema
9. Testing strategy
10. Human-in-the-loop review UI
11. Packaging and deployment
12. Risks and mitigations
13. Timeline
14. Success metrics
15. Immediate next steps

---

## 1. Problem definition

**Input:** Handwritten notebook pages from utility meter surveys (Sachal Survey), photographed on a phone camera. Each page contains 3-4 visual columns and typically 15-30 entries.

**Output:** Excel workbook (`.xlsx`) with one row per meter code, matching the exact format of the reference file `SACHAL_SURVEY_-_IMAGES_DATA.xlsx`.

**Hard constraints:**
- Fully offline (no internet during operation, no cloud APIs)
- Windows is the primary target OS
- CPU-only — no GPU available on the target machine
- Throughput target: 100-300 images per day
- Per-image latency: ~2-5 seconds acceptable
- Deliverable includes a standalone Windows `.exe`

**Non-goals for v1.0:**
- Non-English handwriting (Urdu/Sindhi script, if present, is out of scope)
- PDF input (add in a later version)
- Real-time / watch-folder mode (add in a later version)
- Barcode / QR code recognition (add in a later version)

---

## 2. Data schema

### Input structure (per notebook page)

Each page has three visual columns and occasional side annotations:

| Column | Content | Notes |
|---|---|---|
| Left | Survey number (e.g., `680`) | One per group; blank when meter belongs to previous group |
| Middle | Meter code (e.g., `SFP 93095`) | One per row; space between prefix and digits |
| Right (main) | Free-text remarks | Sparse; e.g., `Shop`, `3 Shops`, `Under Construction`, `Vacant Plot` |
| Right (side) | Annotation codes | Small codes like `+23`, `+37`, `#37`, `127` |

Grouping is indicated visually by hand-drawn brackets (`}`) on the right side of the meter column. All meters visually connected by a bracket share the same survey number.

Special value patterns:
- `No meter` (variants: `No metd`, `no meter`) → transcribed as `NoMeter`
- Ditto marks (`"`, `×`, `''`) meaning "same prefix as row above"
- Corrections: crossed-out digits, arrows (`←`, `→`) confirming correct digits
- Section markers like `End Block "C"`, `× — × — ×` — ignored, not transcribed

### Output schema (Excel workbook)

**Sheet `Data`:**

| Column | Type | Rule |
|---|---|---|
| `SERVEY #` | string | `KE-` + survey number (first row of each group only; blank otherwise) |
| `METER#` | string | Prefix + digits, no space (e.g., `SFP93095`); `NoMeter` for empty plots |
| `REMARKS` | string | Free-text notes; blank when none |
| `ANNOTATIONS` | string | Side codes like `+23`, `+37`, `#37`, `127`; blank when none |

The header spelling `SERVEY #` is preserved as-is from the source workbook (it is a typo but the existing workflow likely references it).

**Sheet `Review` (hidden or filtered):**

Additional columns for the operator review workflow:
- `Page` — source image filename
- `Row` — original row order on the page
- `Confidence` — OCR + validation composite score (0-1)
- `Status` — one of: `ok`, `low_confidence`, `invalid_prefix`, `manual_review`, `error`
- `RawOCR` — original OCR output before post-correction (for debugging)

**Sheet `Summary`:**

Batch statistics: total pages, total meters, average confidence per field, error counts by category.

---

## 3. System architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Input folder (JPG/PNG scans of notebook pages)             │
└──────────────────────────┬──────────────────────────────────┘
                           │
                    Image Loader
                           │
                           ▼
              ┌─────────────────────────┐
              │  Preprocessing          │
              │  - Deskew               │
              │  - Page extraction      │
              │  - Contrast enhance     │
              └────────────┬────────────┘
                           │
                           ▼
              ┌─────────────────────────┐
              │  Layout Detection       │
              │  - Column segmentation  │
              │  - Line detection       │
              │  - Bracket detection    │
              └────────────┬────────────┘
                           │
                           ▼
              ┌─────────────────────────┐
              │  Cell Cropping          │
              │  Per line, per column   │
              └────────────┬────────────┘
                           │
                           ▼
              ┌─────────────────────────┐
              │  HTR Engine (primary)   │
              │  + Fallback for low-    │
              │    confidence cells     │
              └────────────┬────────────┘
                           │
                           ▼
              ┌─────────────────────────┐
              │  Post-Correction        │
              │  - Prefix whitelist     │
              │  - Char confusion fix   │
              │  - Ditto expansion      │
              │  - Regex validation     │
              └────────────┬────────────┘
                           │
                           ▼
              ┌─────────────────────────┐
              │  Grouping & Assembly    │
              │  - Assign survey #      │
              │  - Attach remarks       │
              │  - Attach annotations   │
              └────────────┬────────────┘
                           │
                           ▼
              ┌─────────────────────────┐
              │  Review UI (PySide6)    │
              │  Manual correction      │
              └────────────┬────────────┘
                           │
                           ▼
              ┌─────────────────────────┐
              │  Exporter               │
              │  Excel (openpyxl) + CSV │
              └─────────────────────────┘
```

---

## 4. Tech stack

| Concern | Choice | Rationale |
|---|---|---|
| Language | Python 3.11 | Ecosystem, target compatibility |
| Text detection | PaddleOCR (detection only) | Fast, robust on rotated text |
| Handwriting recognition (primary) | EasyOCR handwritten model | Fast on CPU (~100-200ms/cell), decent accuracy |
| Handwriting recognition (fallback) | TrOCR-small handwritten via `transformers` | Higher accuracy for cells that fail validation |
| Image preprocessing | OpenCV + NumPy | Standard |
| Fuzzy matching | rapidfuzz | Fast Levenshtein / partial ratio |
| Data manipulation | pandas | Standard |
| Excel export | openpyxl | Full control over formatting |
| Config | PyYAML + pydantic | Schema validation |
| Progress display | tqdm (CLI), Qt built-in (GUI) | Standard |
| Logging | loguru | Better ergonomics than stdlib logging |
| GUI | PySide6 (Qt6) | Modern, cross-platform, LGPL |
| Testing | pytest | Standard |
| Packaging | PyInstaller (one-directory mode) | One-file mode is slow on Windows startup |

### On engine choice

Phase 0 evaluates the candidate engines on the six sample pages against ground-truth rows in `SACHAL_SURVEY_-_IMAGES_DATA.xlsx`. Whichever wins on (accuracy × speed) becomes the primary engine. TrOCR-small stays as the fallback for low-confidence cells regardless.

### CPU-only reality check

- EasyOCR handwritten: ~100-200 ms/cell on CPU
- PaddleOCR: ~50-150 ms/cell on CPU
- TrOCR-small (fp32): ~500-1000 ms/cell on CPU
- TrOCR-small (int8 ONNX): ~150-300 ms/cell on CPU

A typical page has 15-25 cells. Using EasyOCR alone: 2-5 sec/page. Using hybrid (EasyOCR + TrOCR fallback on ~20% of cells): 3-6 sec/page. Both fit the daily volume budget.

---

## 5. Domain rules and algorithms

### Meter code pattern

Regex: `^[A-Z]{2,3}\d{4,5}$`

Prefix whitelist (compiled from your samples + reference xlsx):

```
SAA, SAH, SAJ, SAL, SAP, SAT,
SCA, SCC, SCE, SCF, SCG, SCI, SCJ, SCL, SCO, SCP, SCS, SCT, SCU,
SEA, SEE, SEF, SEG, SEL, SEO, SEP, SES, SET, SEU,
SFC, SFE, SFF, SFG, SFH, SFI, SFJ, SFL, SFO, SFP, SFS, SFT, SFU, SFY,
SGA, SGC, SGP,
PSA, PSH,
TT, TY, TZ, SX
```

Stored in `config/meter_prefixes.txt` — one prefix per line, editable without code changes. Any unrecognized prefix at runtime is flagged as `invalid_prefix` for review (it may be a new prefix the OCR got right, or a genuine OCR error).

### Character confusion fix table

Applied only when validation fails on first pass. Try each substitution and re-validate:

```
0 ↔ O ↔ Q
1 ↔ I ↔ l ↔ 7
2 ↔ Z
3 ↔ 8
5 ↔ S
6 ↔ G ↔ b
8 ↔ B ↔ 3
9 ↔ q
```

Position-aware: in a meter code, positions 0-1 (or 0-2) must be letters; the rest must be digits. Example: raw OCR `5FT77430` → position 0 must be a letter, try `S` → `SFT77430` → valid, ship.

### Ditto expansion

Rule: if a cell in the METER# column consists of a ditto/repeat mark (`"`, `''`, `×`, `»`, `”`) followed by digits, replace the mark with the prefix from the previous meter code in the same group.

```python
DITTO_MARKS = ('"', "''", '×', '»', '”')

def expand_dittos(cells):
    last_prefix = None
    for cell in cells:
        text = cell.text.strip()
        if text.startswith(DITTO_MARKS):
            digits_match = re.search(r'\d{4,5}', text)
            if digits_match and last_prefix:
                cell.text = f"{last_prefix}{digits_match.group()}"
                cell.corrected = True
            else:
                cell.status = 'manual_review'
        else:
            prefix_match = re.match(r'^([A-Z]{2,3})', text)
            if prefix_match:
                last_prefix = prefix_match.group(1)
    return cells
```

### KE- prefix and survey number normalization

- Strip whitespace from the survey number cell
- Extract digits: `re.search(r'\d+', cell.text)`
- Prepend `KE-`
- If the row belongs to an existing group (survey cell is blank in original), output blank per the reference xlsx convention

### Grouping algorithm

**Primary method (bracket-based):**
1. Detect right-edge brackets `}` in the meter column via contour analysis
2. Each bracket span defines a group; the leftmost survey number for that span applies

**Fallback method (linear, always available):**
1. Iterate rows top-to-bottom
2. Any row with a non-empty survey number cell starts a new group
3. All subsequent rows without a survey number join the current group

Which method was used per page is logged. If bracket detection quality is poor on your data, the fallback is the shipped default and works correctly for the samples reviewed so far.

### "No meter" handling

- Input pattern: fuzzy match against `no meter` (edit distance ≤ 2, case-insensitive)
- Output: `NoMeter` in the METER# column
- Common accompanying remark: `Vacant Plot`

### Correction detection

Two patterns visible in samples:

**Arrow confirmation** (e.g., `SCE 10194 ← 10194`): the arrow points to the intended value. Rule: if the same numeric sequence appears twice separated by `←`, `→`, or `=`, prefer the pointed-to value.

**Crossed-out with alternative** (e.g., `SFL 836̶ 83628`): OCR both candidates. Prefer the one that validates against the prefix whitelist; if both validate, prefer the last one written left-to-right.

For v1.0: any cell where the raw OCR suggests strike-through or arrow markers is flagged `manual_review` and left for the operator to resolve. Automated correction handling is Phase 2 work if the volume of such cells is high.

### Annotation column extraction

Annotations (`+23`, `+37`, `#37`, `127`) appear in the right side of the page, aligned with specific meter rows. They are:
- Extracted as-is (no normalization)
- Attached to the meter row they are vertically aligned with (nearest-line assignment based on y-coordinate)
- Written to the `ANNOTATIONS` output column
- Pattern: `^[+#]?\d{1,4}$` — captures `+23`, `#37`, `127`

### Section marker filtering

Lines matching any of these patterns are skipped (not transcribed):
- `End Block ["']?[A-Z]["']?`
- Any variant of `× — × — ×` divider
- Page numbers at the bottom
- Column headers repeated on new pages

Stored in `config/section_markers.txt` — one regex per line, editable.

---

## 6. Project folder structure

```
LOCAL_ocr/
├── app.py                          # CLI entry point
├── gui.py                          # PySide6 GUI entry point
├── requirements.txt
├── pyproject.toml
├── README.md
├── LICENSE
├── CHANGELOG.md
│
├── config/
│   ├── settings.yaml               # User-editable config
│   ├── meter_prefixes.txt          # Prefix whitelist
│   └── section_markers.txt         # Ignore-list regexes
│
├── LOCAL_ocr/                     # Main package
│   ├── __init__.py
│   ├── loader/
│   │   └── image_loader.py
│   ├── preprocessing/
│   │   ├── deskew.py
│   │   ├── page_extract.py
│   │   └── enhance.py
│   ├── layout/
│   │   ├── columns.py
│   │   ├── lines.py
│   │   └── brackets.py
│   ├── ocr/
│   │   ├── base.py                 # Engine interface
│   │   ├── easyocr_engine.py
│   │   ├── paddle_engine.py
│   │   └── trocr_engine.py
│   ├── correction/
│   │   ├── prefix_correct.py
│   │   ├── char_confusion.py
│   │   ├── ditto_expansion.py
│   │   └── validator.py
│   ├── grouping/
│   │   ├── bracket_group.py
│   │   └── linear_group.py
│   ├── extraction/
│   │   ├── survey_number.py
│   │   ├── meter_code.py
│   │   ├── remarks.py
│   │   └── annotations.py
│   ├── export/
│   │   ├── xlsx_exporter.py
│   │   └── csv_exporter.py
│   ├── review/
│   │   ├── main_window.py
│   │   ├── image_viewer.py
│   │   └── table_editor.py
│   ├── config/
│   │   └── schema.py               # pydantic models
│   └── logging_setup.py
│
├── models/                         # Downloaded HTR models (created at runtime)
│
├── samples/                        # Reference images + ground truth
│   ├── images/
│   └── ground_truth.xlsx
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── data/
│
├── scripts/
│   ├── eval_engines.py             # Phase 0 evaluator
│   ├── package_windows.py          # PyInstaller wrapper
│   └── download_models.py          # First-run model download
│
└── docs/
    ├── user_manual.md
    ├── troubleshooting.md
    ├── config_reference.md
    └── adding_prefixes.md
```

---

## 7. Phase-by-phase build plan

### Phase 0 — Engine evaluation (Days 1-4)

**Goal:** Pick the OCR engine with the best real-world accuracy-per-millisecond on your data.

Tasks:
1. Set up dev environment (Python 3.11 venv, install PaddleOCR, EasyOCR, transformers)
2. Build a ground-truth CSV from the 6 sample pages plus matching rows from the reference xlsx
3. Manually crop each cell from the 6 pages (rough bounding boxes are fine)
4. Write `scripts/eval_engines.py`:
   - Load each cropped cell
   - Run each engine on it
   - Compute per-cell exact-match accuracy against ground truth
   - Compute post-correction accuracy (apply prefix whitelist + char confusion)
   - Measure per-cell latency
5. Produce evaluation report

**Deliverable:** `docs/engine_eval_report.md` with a comparison table:

| Engine | Raw accuracy (meter codes) | Post-correction | Latency (ms/cell) |
|---|---|---|---|
| PaddleOCR | ? | ? | ? |
| EasyOCR | ? | ? | ? |
| TrOCR-small | ? | ? | ? |

**Exit criteria:** Winner declared with data.

### Phase 1 — Core pipeline MVP (Week 1-2)

**Goal:** End-to-end pipeline from image folder to xlsx, using the chosen engine, no GUI.

Tasks:
1. Image loader with format validation
2. Basic preprocessing: grayscale, deskew via `cv2.minAreaRect`
3. Page extraction (crop notebook from background using contour detection)
4. Column region detection (x-coordinate binning based on text density)
5. Line detection (horizontal projection profile)
6. Cell cropping
7. HTR engine wrapper (winner from Phase 0)
8. Basic post-correction: prefix whitelist match + character confusion fix
9. Linear grouping (fallback method — bracket detection comes later)
10. `KE-` prefix application + ditto expansion
11. xlsx exporter matching the reference format exactly
12. CLI: `python app.py --input <folder> --output <file.xlsx>`
13. Structured logging with loguru

**Deliverable:** Working CLI that processes a folder end-to-end.

**Exit criteria:** Process all 6 sample images and produce xlsx that matches ground-truth rows with ≥75% meter-code accuracy.

### Phase 2 — Accuracy improvements (Week 3)

**Goal:** Close the accuracy gap through smarter post-correction and hybrid HTR.

Tasks:
1. Fallback HTR: if primary engine confidence < threshold or result fails validation, re-OCR that cell with TrOCR-small
2. Fuzzy prefix matching with rapidfuzz (score every 3-letter candidate against whitelist, threshold ≥ 85)
3. Bracket detection for improved grouping
4. Correction handling: detect arrows and crossouts (or flag for review)
5. `ANNOTATIONS` column extraction (side codes like `+23`, `#37`, `127`)
6. Remarks column extraction with y-alignment row assignment

**Deliverable:** Same pipeline, higher accuracy.

**Exit criteria:** Meter-code accuracy ≥ 90% on the labeled sample set; annotations and remarks correctly placed.

### Phase 3 — Configuration and CLI polish (Week 4)

**Goal:** Production-quality CLI usable by someone who didn't write it.

Tasks:
1. YAML config loader with pydantic schema validation
2. CLI arguments override config values
3. Multi-process batch processing (`ProcessPoolExecutor` — HTR models don't thread-share)
4. tqdm progress bar with ETA
5. Streaming architecture: never load all images into memory at once
6. Graceful Ctrl+C — save partial results
7. Error taxonomy: `unreadable`, `no_text_found`, `ocr_failed`, `validation_failed`, `export_failed`
8. Per-image error records in the output (nothing silently dropped)

**Deliverable:** Robust CLI ready for daily use.

**Exit criteria:** Process a 300-image batch with stable memory, accurate progress, and complete error accounting.

### Phase 4 — Excel output quality (Week 4-5)

**Goal:** Output workbook that matches your existing format and is production-ready.

Tasks:
1. Match reference xlsx exactly: same headers, same blank-survey convention
2. Add `ANNOTATIONS` column
3. Second sheet `Review` with confidence, page, row, status, RawOCR columns
4. Third sheet `Summary` with per-batch statistics
5. Cell formatting: confidence-based color coding on the Review sheet (green/yellow/red)
6. Freeze header row, auto-width columns
7. Backup previous output before overwriting

**Deliverable:** Production-quality xlsx output.

### Phase 5 — Human-in-the-loop review UI (Week 5-7)

**Goal:** Efficient manual correction interface. This is where the last 10% of accuracy comes from.

Tasks:
1. PySide6 main window: page image on left (~60% width), extracted table on right (~40%)
2. Image viewer: pan, zoom, rotate; overlay bounding boxes on detected cells
3. Table editor: editable QTableView bound to a pandas DataFrame
4. Click a table cell → jump to and highlight the corresponding region on the image
5. Low-confidence cells highlighted yellow, invalid cells red
6. Keyboard-first workflow: arrow keys navigate, Tab accepts, Enter commits, Ctrl+Z undoes
7. "Review-only" mode: skip cells with confidence above configurable threshold
8. Correction log: every edit written to `logs/corrections.jsonl` (becomes training data if you fine-tune later)
9. Autosave: overwrite the xlsx as edits are made (with a backup file)
10. Batch mode: PgUp/PgDn navigate between pages

**Deliverable:** GUI usable by a non-technical operator.

**Exit criteria:** Operator processes a 50-page notebook in the time it would take to type 20 pages manually.

### Phase 6 — Testing (Weeks 7-8, overlapping)

Tasks:
1. Unit tests for every module in `LOCAL_ocr/`:
   - `preprocessing`: known-good input → known-good output
   - `layout`: pixel counts, region coordinates
   - `correction`: table-driven tests (given raw text, expect fixed text)
   - `grouping`: given cell list, expect group assignments
   - `extraction`: given text, expect structured fields
   - `export`: given records, expect valid xlsx
2. Integration tests: full pipeline on 3-5 golden pages, assert xlsx matches expected
3. Accuracy regression: run on labeled sample set, fail if accuracy drops below Phase 2 target
4. Performance regression: assert per-image latency stays within budget
5. Edge case coverage:
   - Empty folder
   - Corrupt image file
   - Duplicate filenames
   - Unicode paths (important on Windows)
   - Extremely large image
   - Image with no text
   - Image rotated 90°/180°
6. Target: >80% coverage on core modules

**Deliverable:** Test suite runnable via `pytest`.

### Phase 7 — Packaging and deployment (Week 8-9)

Tasks:
1. PyInstaller spec file, one-directory mode (not one-file — startup speed matters)
2. Model download strategy:
   - **Recommended: lazy download on first run** (needs one-time internet)
   - Store models in `%LOCALAPPDATA%\LOCALOCR\models\`
   - Alternative: bundle in the exe (~500 MB - 1 GB, slow install)
3. Test on a clean Windows 10/11 VM with no Python installed
4. Handle VC++ redistributable dependencies (auto-detect and prompt if missing)
5. Windows shortcut with correct working directory
6. Optional: NSIS installer wrapper

**Deliverable:** Standalone Windows exe + installer.

**Exit criteria:** Fresh Windows machine runs the exe, processes a sample folder, produces correct output.

### Phase 8 — Documentation (Week 9)

Tasks:
1. `README.md`: quickstart, install, first run
2. `docs/user_manual.md`: full walkthrough with screenshots
3. `docs/troubleshooting.md`: common issues (missing models, bad photos, low accuracy)
4. `docs/config_reference.md`: every YAML setting explained
5. `docs/adding_prefixes.md`: how to update `meter_prefixes.txt` when new prefixes appear
6. In-code docstrings on public APIs
7. `CHANGELOG.md` starting from v1.0

**Deliverable:** Complete docs bundle.

---

## 8. Configuration file schema

`config/settings.yaml`:

```yaml
input:
  folder: "./images"
  formats: [jpg, jpeg, png, bmp, tiff]
  recursive: false

output:
  folder: "./output"
  xlsx: true
  csv: false
  filename_pattern: "LOCAL_{date}_{time}.xlsx"
  backup_previous: true

ocr:
  primary_engine: easyocr        # Set from Phase 0 result
  fallback_engine: trocr_small
  fallback_threshold: 0.65       # Confidence below this triggers fallback
  languages: [en]

preprocessing:
  deskew: true
  extract_page: true
  enhance_contrast: true
  denoise: false

domain:
  survey_prefix: "KE-"
  prefix_whitelist_file: "./config/meter_prefixes.txt"
  section_markers_file: "./config/section_markers.txt"
  meter_pattern: "^[A-Z]{2,3}\\d{4,5}$"
  fuzzy_prefix_threshold: 85

processing:
  workers: 4                     # Process pool size (match CPU cores)
  batch_size: 50
  min_confidence: 0.60

logging:
  level: INFO
  file: "./logs/LOCAL_{date}.log"
  rotate_daily: true

gui:
  review_threshold: 0.85         # Cells above this auto-accepted in review mode
  autosave_interval_seconds: 30
```

---

## 9. Testing strategy

### Golden dataset

Curate 20-30 pages with hand-verified ground truth. Store in `samples/`:
- `samples/images/page_001.jpg` etc.
- `samples/ground_truth.xlsx` — one sheet per page with expected rows

Both the Phase 0 evaluation script and the accuracy regression test consume this dataset. Grow it over time as new edge cases surface.

### Accuracy metrics

For each field type (survey #, meter #, remarks, annotations), compute:
- **Exact match rate**: percentage of cells where OCR + post-correction equals ground truth
- **Edit distance**: mean Levenshtein distance per cell
- **Validation pass rate**: percentage of cells matching the expected pattern

Report per-field and overall.

### Performance metrics

- Per-image latency: p50, p95, p99
- Peak memory during a 300-image batch
- Cumulative processing time for a full batch

### Regression testing

Run accuracy and performance tests on every commit. Fail the build if:
- Any field's accuracy drops more than 2 percentage points from the last release
- p95 latency exceeds the configured budget

---

## 10. Human-in-the-loop review UI details

### Layout

```
┌──────────────────────────────────────────────────────────┐
│  File  Edit  View  Batch  Help                           │
├──────────────────────────────┬───────────────────────────┤
│                              │  Page: page_012.jpg (12/50)│
│                              ├───────────────────────────┤
│                              │  SERVEY# │ METER# │ REM..│
│      [Page image with        │  KE-680  │ TY51697│      │
│       bounding box           │          │ PSA... │      │
│       overlays]              │          │ SFP... │      │
│                              │  KE-681  │ SFC... │      │
│                              │  ...     │ ...    │ ...  │
│                              │                          │
├──────────────────────────────┴───────────────────────────┤
│ Status: 12/50 pages | 3 flagged | Confidence avg: 0.87  │
└──────────────────────────────────────────────────────────┘
```

### Interaction model

- Click a table cell → viewer pans/zooms to the corresponding image region
- Cells color-coded by status: green (auto-accepted), yellow (low confidence), red (invalid)
- Tab moves to next flagged cell, skipping the rest
- All edits logged to `logs/corrections.jsonl`
- Autosave every 30 seconds; also on page change

### Why this matters

For handwritten data, review UI quality drives the effective delivered accuracy far more than the OCR model choice does. A great review flow with a mediocre OCR beats a great OCR with a bad review flow. Budget serious time here.

---

## 11. Packaging and deployment

### PyInstaller specifics

- One-directory mode, not one-file (startup speed)
- Explicitly include `paddleocr`, `easyocr`, `transformers` data directories via `--add-data`
- Exclude unused torch backends to shrink size
- Test on the target Windows version early — do not save this for the final week

### Model bundling

**Recommended:** Lazy download on first run. Store models in `%LOCALAPPDATA%\LOCALOCR\models\`. Requires one-time internet at install time. Show a progress dialog.

**Alternative:** Bundle models with the exe. Adds ~500 MB - 1 GB but works with zero internet. Choose this if the target machine truly cannot reach the internet even once.

### Windows-specific concerns

- Long paths (>260 chars) — enable long path support or warn on install
- Unicode filenames — test with Urdu/Arabic characters in image filenames
- VC++ redistributable dependency — auto-detect and offer to install
- Antivirus false positives on PyInstaller builds — sign the exe if possible

---

## 12. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Handwriting accuracy too low even after post-correction | High — project fails | Phase 0 evaluation with real data before commitment; strong review UI as fallback |
| TrOCR too slow on CPU | Medium — misses latency target | ONNX export + int8 quantization; fall back to EasyOCR-only if needed |
| Bracket detection unreliable | Medium — bad groupings | Linear fallback method built first; bracket detection is a Phase 2 enhancement, not a v1 blocker |
| New meter prefixes appear that aren't in whitelist | Medium — false rejections | Config file whitelist, easy to update; unrecognized prefixes flagged for review, not dropped |
| Photo quality varies wildly (shadows, tilt, blur) | Medium — accuracy drift | Preprocessing pipeline handles common issues; capture guidelines in user manual |
| PyInstaller model bundling breaks on Windows | Low — packaging issue | Lazy model download instead of bundling; test on clean VM from Phase 4, not Phase 7 |
| Data confidentiality concerns | Low but must be zero | Fully offline architecture, no network calls anywhere; document this explicitly in the manual |
| Corrupted image crashes the batch | Low | Per-image try/except; corrupted files logged and skipped, not fatal |
| Annotations column ambiguity (which meter row does `+37` attach to?) | Medium — data quality | Y-alignment heuristic; flag ambiguous cases for review |

---

## 13. Timeline

Solo developer estimate:

| Phase | Days (full-time) | Weeks (part-time evenings) |
|---|---|---|
| 0. Engine evaluation | 3-4 | 1 |
| 1. Core pipeline | 8-10 | 2-3 |
| 2. Accuracy improvements | 5 | 1.5 |
| 3. CLI polish | 4 | 1 |
| 4. Excel output | 3 | 1 |
| 5. Review UI | 10-12 | 3-4 |
| 6. Testing | 5 | 1.5 |
| 7. Packaging | 4 | 1 |
| 8. Documentation | 3 | 1 |
| **Total** | **45-55 days** | **12-15 weeks** |

Full-time: ~2 months. Part-time evenings and weekends: ~3-4 months. Add a 20% buffer for real-world debugging on your specific data.

---

## 14. Success metrics

The project ships when:

1. Meter code accuracy on a labeled test set (20+ pages) reaches ≥90% post-correction, pre-review
2. After human review of flagged cells, effective delivered accuracy reaches ≥99%
3. A 300-image batch processes in ≤30 minutes on the target CPU
4. Peak memory stays under 2 GB during a 300-image batch
5. A non-technical operator completes the review workflow without needing to consult docs
6. A fresh Windows machine runs the packaged exe with no errors, given only the installer

---

## 15. Immediate next steps

1. **Confirm the plan** — any phase you want to reorder, add, or drop
2. **Confirm the meter prefix whitelist** — I compiled it from what I saw in samples and the reference xlsx; the utility may have a canonical list that catches any I missed
3. **I write `scripts/eval_engines.py`** (the Phase 0 deliverable). You run it on your Windows machine and share the numbers.
4. **Once the engine winner is chosen**, I write Phase 1 module by module, and you test each against real images as we go.

---

*End of implementation plan.*
