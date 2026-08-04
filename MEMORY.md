# MEMORY.md — project context & decisions

A durable record of decisions and hard-won facts that aren't obvious from the
code alone. Newest context at the top of each section.

## Naming

- The app is **Servey OCR** (repo: `Servey_OCR`). "Sachal" is **not** the app
  name — it's the surveyed area, and survives only in the real source-data
  filename `SACHAL SURVEY - IMAGES DATA.xlsx`, which must not be renamed.

## Two recognition modes, one flow

- The app has **two modes** sharing the flow
  `upload → recognise → correction → review/edit → export`:
  - **Gemini AI** (default) — cloud, `app/recognizer.py`.
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

## Known limitations

- The bootstrap segmenter (`local_ocr/layout/`) is not usable unsupervised
  (right page 9/12, row counts often wrong). It's only for the optional local
  engines; Gemini does its own layout, so this doesn't affect normal use.
