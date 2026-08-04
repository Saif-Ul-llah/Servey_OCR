# Servey OCR

Turn photographs of a handwritten meter-survey notebook into an `.xlsx` that is
structurally identical to the reference workbook
`Samples or examle data/SACHAL SURVEY - IMAGES DATA.xlsx` — with a desktop app
for upload → recognise → review → export, and a deterministic core that refuses
to guess silently.

The design goal is **no silent errors**: every value is either verified correct
or explicitly flagged for a human to check.

---

## Quick start (desktop app)

```bash
python -m pip install -r requirements.txt
python run_app.py            # or double-click run_app.bat on Windows
```

The app opens at `http://127.0.0.1:8000` (loopback only — nothing is exposed to
the network). On first run, click **Settings**, paste a
[Gemini API key](https://aistudio.google.com/app/apikey), **Test**, **Save**.

### Or a standalone `.exe` (no Python needed)

**Double-click and the app opens in your browser.** Two builds, because the
local OCR stack is most of the weight:

| Executable | Size | Modes | Needs |
|---|---|---|---|
| `dist\ServeyOCR.exe` | ~45 MB | Gemini, Grok | an API key + internet |
| `dist\ServeyOCR-Offline.exe` | ~1–2 GB | **all three**, incl. Local OCR | nothing — fully offline |

The offline build bundles OpenCV, PyTorch and EasyOCR *with its weights*, so it
never downloads anything on first run. It starts more slowly (it unpacks itself)
and the first local OCR run pays a one-off model-load cost.

Each build knows what it can do: the lean one opens in Gemini mode and greys out
Local OCR with an explanation, rather than offering a mode that would fail.

Rebuild with `build_exe.bat` (it asks which build), or directly:

```bash
python -m PyInstaller ServeyOCR.spec --noconfirm            # lean
python -m PyInstaller ServeyOCR-Offline.spec --noconfirm    # offline
```

- Keep the exe in its own folder: your saved key (`app_settings.json`) and the
  `output\` folder are written next to it.
- First launch is slower (it unpacks itself — noticeably so for the offline
  build), and Windows SmartScreen/Defender may prompt because the exe is
  unsigned — choose *More info → Run anyway*.

---

## How it works

Three recognition modes, chosen with the toggle in the top bar. **All follow the
same flow** — `upload images → recognise → correction → review/edit → export` —
and differ only in *which* recognizer reads the pages:

| Mode | What reads the pages | Needs |
|---|---|---|
| **Local OCR** *(default)* | The local segmenter crops each cell and a local engine (EasyOCR) reads it — no cloud. | engine installed (see below); fully offline |
| **Gemini** | Google Gemini reads each page (layout + handwriting together) into raw rows. | API key + internet |
| **Grok** | xAI Grok, same job via an OpenAI-compatible vision endpoint. | API key + internet |

Local OCR is the default because it works with no API key and no network. The
cloud modes are more accurate on these images; switch with the top-bar toggle
once you have pasted a key into Settings.

Each cloud provider keeps its own key and model in Settings, and **Test key**
confirms both against the live API before you rely on them. Model names are
free-text with suggestions, because providers rename models often.

Whichever mode you use, every value then flows through one deterministic core:

```
raw rows ─▶ correction ─▶ cross-page assembly ─▶ audit ─▶ XLSX export
           (grammar,       (page ordering,       (gaps,
            prefix repair,   group healing,        duplicates,
            ditto marks,     remark binding)       row counts)
            side codes)
```

Rows the rules are unsure about are highlighted for review, with one-click
alternative readings. The exported workbook is byte-compatible with the
reference file (same sheet, headers, column widths, and — critically — the same
cell *types*: an empty continuation-survey cell is genuinely empty, a lone side
code is a number).

### Local OCR (offline) mode

Runs with no cloud: `local_ocr/layout` segments the page, `local_ocr/ocr` reads
each cell, then the same correction/assembly/audit runs. Install an engine into
the Python environment first (TrOCR is the only true handwriting model):

```bash
python -m pip install easyocr             # the default engine
# optional alternatives:
python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
python -m pip install transformers        # TrOCR-base
```

Pick the engine in **Settings → Local OCR engine**. The default is **EasyOCR**,
chosen by measurement rather than reputation — on page_08 against the golden set:

| Engine | Codes exactly right | Digit strings right | Speed |
|---|---|---|---|
| **EasyOCR** | **3 / 7** | **5 / 7** | **0.65 s/cell — page in ~20 s** |
| TrOCR-base | 0 / 7 | 3 / 7 | ~4 s/cell — page in ~10 min |

TrOCR is the "handwriting" model, but this hand is neat block capitals and
digits — closer to the printed signage EasyOCR was trained on than to the
English prose TrOCR expects, which is why TrOCR reached for words like
`CONTEMPTATION`. EasyOCR also takes the field alphabet as a decoder allowlist,
and its confidence tracks correctness closely enough to drive review flagging:
in that run every correct code auto-accepted and every wrong one was flagged.

Two things make this mode workable rather than useless, both measured on the
golden set:

- **Rule-line anchored segmentation** (`local_ocr/layout/ruled.py`). Rows are the
  bands between the notebook's *printed* rules, not bands of ink, so the row
  count no longer depends on how much was written. The page being transcribed is
  found by clustering ink columns — the fold is simply the blank band between
  blocks — and the three column boundaries are placed at the ink "valleys". The
  earlier fold-hunting approach cut straight through the meter column and handed
  the recogniser half-codes.
- **Grammar-constrained decoding** (`local_ocr/ocr/constrained.py`). Left free,
  a handwriting model shown an isolated code returns the nearest English *word*
  (`SCL 93513` came back as `CONTEMPTATION`). Decoding is restricted to the
  field's grammar — digits for survey; one of the 65 whitelisted prefixes then
  exactly five digits for meter — which makes a structurally impossible reading
  unrepresentable. On a sample page this took valid prefixes from 0/7 to **7/7**.

Caveats, stated plainly:

- **Use TrOCR-base, not -small.** The `-small` checkpoint ships a SentencePiece
  tokenizer that transformers 5.x cannot load on Python 3.14; `-base` uses a BPE
  tokenizer that loads fine (the engine builds it directly, bypassing the broken
  converter).
- **It is still the least accurate mode.** On the WhatsApp-compressed samples
  (~18px glyphs) digits come out right roughly half the time, so expect real
  work in the review grid. The cloud modes read these pages far better. Local OCR
  is the right choice when offline is a hard requirement, or once
  full-resolution originals are available.

- **Accuracy is capped by the segmenter**, which is a bootstrap and is rough on
  the WhatsApp-compressed samples (better on full-res originals). The review
  grid is where you catch its mistakes; low-confidence cells are flagged.
- **Not in the lean `.exe`** — use `ServeyOCR-Offline.exe` or `python run_app.py`.

**Why a cloud model is the default:** a local 3B *vision* model reserves ~10 GB
at runtime and this 8 GB machine can't hold it. Gemini and Grok read the whole
page in one call and need no local segmentation at all. Local OCR engines
(TrOCR/EasyOCR/Paddle) are far lighter and do run here, just slower and gated by
segmentation quality.

---

## What the reference workbook taught us

Facts enforced in code that would otherwise be guesswork:

- meter digits are **always exactly 5** (857 of 862 parsable codes; only `LA…`
  carries 6), making a meter code an 8-character string over a closed set of 65
  prefixes;
- the survey column carries `KE-nnn` on a group's first row only, and the cell
  is **genuinely empty** on continuation rows, not an empty string;
- side codes written `+37` / `#37` are stored as `-37`; a lone code is a
  **number**, anything else is text (`'-42,-128'`, `'-37,SHOP'`);
- a free-text remark binds to its group's first row — which may be on the
  *previous* photograph — unless it shares a line with a side code, where it
  stays put;
- repeat marks (`"`) inherit from the line physically above, crossing survey
  group boundaries;
- Urdu remarks on the pages are dropped from the workbook;
- survey numbers run consecutively and meter codes are globally unique, so
  missed pages, duplicated pages and misreads are detectable with no image
  analysis at all.

---

## Layout

```
app/               desktop app
  recognizer.py    Google Gemini cloud recognition (REST)
  pipeline.py      drives correction -> assembly -> export from UI rows
  server.py        FastAPI JSON API + serves the UI
  settings_store.py persisted Gemini key / model / mode
  static/          index.html, styles.css, app.js (single-page UI)
local_ocr/         deterministic core (no cloud, no UI)
  correction/      grammar, whitelist repair, repeat marks, placeholders, remarks
  assembly/        page ordering, cross-page groups, remark binding, audits
  export/          3-column deliverable + review sidecar
  layout/          bootstrap segmenter (optional local engines only)
  ocr/             local engine interface + adapters (optional)
config/            settings.yaml, generated prefix whitelist, marker + token tables
samples/           page manifest, ground truth, staged images
scripts/           derive_prefixes, build_golden_set, make_crops, eval_engines
run_app.py         launch the app        ServeyOCR.spec   build the .exe
tests/             unit + integration    AGENTS.md         guide for contributors/agents
```

---

## Development

```bash
python -m pytest tests            # 154 tests
```

`tests/integration/test_export_golden.py` runs correction, assembly and export
over the whole 287-row golden set with a simulated perfect recognizer and
compares the result cell-for-cell — including cell *types* — against reference
workbook rows 596–882. This is the guard that proves the rules never corrupt a
correct reading.

For the optional local (offline) engine bake-off, install a recognizer and see
`scripts/make_crops.py` / `scripts/eval_engines.py`. Regenerate the derived
config with `scripts/derive_prefixes.py` and `scripts/build_golden_set.py`.

See **AGENTS.md** for architecture conventions and the non-negotiable rules when
changing this code.
