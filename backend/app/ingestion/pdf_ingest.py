"""Reading a PDF or a photo into a recipe. Split into three modules (0186),
one per job:

- pdf_read: a PDF's text layer page by page, and its largest embedded image;
- ocr: Tesseract on a page or photo, its layout, two-column lists;
- segment: a text's lines sorted into title, ingredients and steps (the
  built-in rules and the learned model), within the size limits.

This module keeps the old import path: `from .pdf_ingest import X` and
`pdf_ingest.X` find X in whichever of the three holds it now, looked up
each time, so values that change at run time (the learned model) read
as they are. New code imports from the module that holds the name. A
test that replaces a function patches it there too: replacing it here
wouldn't change the code that calls it.
"""
from . import ocr, pdf_read, segment

_MODULES = (pdf_read, ocr, segment)


def __getattr__(name):
    for module in _MODULES:
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted({n for m in _MODULES for n in dir(m) if not n.startswith("__")})
