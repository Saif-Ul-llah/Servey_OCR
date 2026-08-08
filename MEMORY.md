# MEMORY.md — project context & decisions

A durable record of decisions and hard-won facts that aren't obvious from the
code alone. Newest context at the top of each section.

## Naming

- The app is **Servey OCR** (repo: `Servey_OCR`). "Sachal" is **not** the app
  name — it's the surveyed area, and survives only in the real source-data
  filename `SACHAL SURVEY - IMAGES DATA.xlsx`, which must not be renamed.

## Provider model names (verified against the live APIs, Aug 2026)

- **xAI Grok:** `grok-2-vision-1212`, `grok-2-vision`, `grok-vision-beta`,
  `grok-beta`, `grok-2-latest` are all **retired — they 404**. Live names:
  `grok-4`, `grok-4-fast` (default), `grok-4-latest`, `grok-3`. xAI validates the
  model name *before* the key, so an invalid key still distinguishes
  "Model not found" from "Incorrect API key" — a cheap way to probe valid names.
- Because names churn, both providers' model fields are **free text with
  suggestions**, not closed dropdowns, and Settings has a Test button.

## Three recognition modes, one flow

- The app has **three modes** sharing the flow
  `upload → recognise → correction → review/edit → export`:
  - **Gemini** (default) and **Grok** — cloud, both in `app/recognizer.py`
    behind one provider table; each has its own key + model.
  - **Local OCR** (offline) — `app/local_engine.py`: the `local_ocr.layout`
    segmenter crops cells, a `local_ocr.ocr` engine (**TrOCR-base** default)
    reads them. Chosen in Settings. "Manual typing" is NOT the offline mode —
    the engine does the reading; the user only reviews/edits. Low-confidence
    cells are flagged for review (`pipeline._flag_low_confidence`).
  - Needs `torch`+`torchvision`+`transformers` installed; **not** in the lean
    `.exe` (PyTorch too big) — use `python run_app.py`. Accuracy is capped by the
    bootstrap segmenter (rough on low-res samples).
  - **TrOCR tokenizer gotcha (Python 3.14 + transformers 5.x):** transformers
    5.14 is the only version with 3.14 wheels, and its slow→fast tokenizer
    converters are broken. `trocr-small` (SentencePiece tokenizer) can't load;
    `trocr-base` (BPE, ships `vocab.json`+`merges.txt`) works because
    `local_ocr/ocr/engines.py::_load_trocr_processor` builds the fast tokenizer
    directly from those files. Don't switch the default back to small here.

## Recognizer default: Google Gemini (cloud)

- Recognition runs on **Google Gemini** via `app/recognizer.py` (REST, no SDK).
  The user supplies their own API key in-app; it's stored in
  `config/app_settings.json` (dev) or beside the `.exe` (frozen) and is
  git-ignored.
- **Local VLM was dropped.** On this 8 GB machine, `qwen2.5vl:3b` reserves
  ~10 GB at runtime (the 3.2 GB is only the on-disk file) and never loads —
  every request timed out. Ollama model removed, `scripts/vlm_probe.py` deleted.
  Do not re-propose a local VLM here; a small local OCR engine (TrOCR/Paddle) is
  the offline path if ever needed.
- **Not a RAM-reclaim target:** the "4 GB shared GPU memory" Windows shows is a
  dynamic ceiling, not reserved RAM. Only ~0.5 GB (the UMA frame buffer) is
  truly carved out. The real fix for local models was closing apps (Chrome held
  ~3 GB) — moot now that recognition is cloud.

## Deterministic core is the authority

- `local_ocr/` is unchanged by the app and covered by **154 tests**. Gemini only
  fills raw cells; correction → assembly → audit → export decides the output.
- The golden round-trip proves the rules never corrupt a correct reading:
  feeding all 287 golden rows through correction with a perfect recognizer
  reproduces reference rows 596–882 cell-for-cell, including cell *types*.
- Digit-to-digit "correction" is deliberately **not** auto-applied — with no
  master meter list it would manufacture plausible, unverifiable errors. Only
  checkable fixes (position/type, prefix repair) auto-apply; the rest go to
  review.

## App-layer specifics

- `app/pipeline.py` strips a leading `KE-` from the survey field before
  correction — otherwise digit-coercion reads the letters as digits
  (`KE-683` → `KE-3683`). Gemini is also prompted to emit survey as digits only.
- Export writes the reviewed grid **verbatim** (only re-coercing remark cell
  types), so a human's edits are never second-guessed.
- Paths are PyInstaller-frozen-aware (`local_ocr/paths.py`): read-only bundle vs
  writable data-next-to-exe.

## Segmentation: what actually works (and what does not)

Measured with `scripts/eval_segmentation.py` against the manifest's known row
counts. `local_ocr/layout/ruled.py` supersedes `bootstrap.py` for local OCR.

- **Do not hunt for the fold.** It is not the darkest column (the page's own
  shadow wins) and not the biggest break in the ruling — handwriting sitting on
  the rules breaks *them* and manufactures a deeper trough in the middle of the
  text. That put the split through the meter column and produced half-codes;
  the crops fed to the model were literally blank paper.
- **Do cluster ink columns instead.** The fold is the blank band between blocks;
  the densest block is the page. Column boundaries are the ink "valleys",
  scored against the *strongest* neighbouring column (against the weakest, a
  chance dip inside the meter column beats the real boundary).
- **Rows are the bands between printed rules**, found as wide-thin connected
  components (a short opening kernel — pages are photographed at an angle, so a
  long straight kernel misses tilted rules), then regularised on the median
  pitch to fill missed lines, then phase-shifted so boundaries fall between
  lines of writing rather than through them.
- **Bias to over-detect rows.** Assembly drops rows with no ink in any column,
  so a spare blank band costs nothing while a missed one silently loses a meter.

## Local OCR engine: EasyOCR, chosen by measurement

- **EasyOCR beats TrOCR here**, despite TrOCR being the "handwriting" model.
  page_08 vs golden set: EasyOCR 3/7 codes exact, 5/7 digit strings, 0.65s/cell;
  TrOCR-base 0/7 exact, 3/7 digits, ~4s/cell. The hand is neat block capitals
  and digits — scene-text territory, not English prose. TrOCR's word bias is
  exactly why it produced `CONTEMPTATION`.
- EasyOCR's **confidence is calibrated** (high = right, low = wrong), so the
  low-confidence review flag actually works. Do not swap it for an engine whose
  confidence is uninformative without re-checking that.
- **Skip blank cells before inference** (`app/local_engine._is_blank`). Rows are
  over-detected on purpose, so ~25% of cells are empty. Test against the
  *rule-subtracted* ink mask — thresholding the raw crop calls every cell inked,
  because the printed ruling is darker than the paper.
- Page time went 598s → 20s from these two changes together.

## Two .exe builds

- `ServeyOCR.spec` -> lean (~45 MB), cloud modes only.
- `ServeyOCR-Offline.spec` -> ~1-2 GB, bundles OpenCV + torch + EasyOCR
  **and EasyOCR's weights** from `~/.EasyOCR/model` (an "offline" app that
  downloads 94 MB on first use is not offline). Needs `collect_all` for those
  packages — following imports alone leaves the frozen build broken at runtime.
- **Never key behaviour off `sys.frozen`** now that both builds exist. Probe for
  what is actually present (`importlib.util.find_spec("cv2")`) to decide the
  default mode and local-OCR availability; the lean exe must not open in a mode
  it cannot run, the offline exe should.
- `app/local_engine.py` imports cv2 and the layout stage **lazily**. At module
  scope, the lean exe fails to start entirely, because `server.py` imports it.
- The static UI is baked into the exe, so **a CSS/JS fix needs a rebuild** to
  reach exe users.

## UI gotcha that made the app look frozen

- `[hidden] { display: none !important; }` must stay in `styles.css`. A class
  selector (`.overlay { display:flex }`) outranks the browser's own `[hidden]`
  rule, so without it the loading overlay and settings modal paint over the app
  from first render and nothing is clickable — it looks like a stuck "Working…".
- **Render the page in a browser before calling UI work done.** This bug was
  invisible to HTTP-level endpoint tests, which all passed.

## Known limitations

- Local OCR accuracy on the WhatsApp-compressed samples is still modest
  (~half the digit strings exact) — the glyphs are ~18px. Cloud modes are much
  better on this data. Re-check segmentation thresholds when full-resolution
  originals arrive; the approach is structural, but the constants were fitted at
  960x1280.
