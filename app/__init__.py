"""Local desktop web app for the Servey OCR pipeline.

A thin FastAPI layer over the existing `local_ocr` pipeline: upload page
photographs, recognise them with Google Gemini (or type them in by hand),
review and correct the result in an editable grid, and export the reference
workbook. The deterministic correction / assembly / export core is unchanged;
this package only adds recognition (cloud) and a user interface.
"""

__version__ = "1.0.0"
