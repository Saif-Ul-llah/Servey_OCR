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

### Or the standalone `.exe` (no Python needed)

`dist\ServeyOCR.exe` is a single self-contained executable — **double-click it**
and the app opens in your browser. Rebuild after code changes with
`build_exe.bat` (or `python -m PyInstaller ServeyOCR.spec --noconfirm`).

- Keep the exe in its own folder: your saved key (`app_settings.json`) and the
  `output\` folder are written next to it.
- First launch is a few seconds slower (it unpacks itself), and Windows
  SmartScreen/Defender may prompt because the exe is unsigned — choose
  *More info → Run anyway*.

---

## How it works

Two recognition modes, chosen with the toggle in the top bar:

| Mode | What it does | Needs |
|---|---|---|
| **Gemini AI** | Google Gemini reads each page (layout + handwriting together) into raw rows. | API key + internet |
| **Manual** | You type the rows yourself; *Validate & correct* runs the same rules. | nothing — fully offline |

Either way, every value then flows through one deterministic core:

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

**Why cloud, not a local model?** A local 3B vision model reserves ~10 GB at
runtime; this 8 GB machine can't hold it. Gemini decouples recognition accuracy
from the laptop's RAM. The local OCR engine adapters (TrOCR/Paddle/EasyOCR) and
the bootstrap segmenter remain in the tree for a fully-offline recognizer later,
but are optional and not required.

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
