# pdf_to_epub

Convert PDFs to clean, well-structured EPUB 3 ebooks using the **DeepSeek LLM** for formatting.

The converter extracts text from each PDF page (native text layer, with OCR fallback for scans), splits the book into chunks, and sends them to the DeepSeek API in parallel. The LLM reformats the raw text into proper XHTML chapters — fixing paragraphs, chapter boundaries, and OCR artifacts — which are then assembled into a validated EPUB 3.

The LLM is the **only** conversion path: a DeepSeek API key is always required.

## Folder structure

```
pdf_to_epub/
├── input/                  # Drop your PDFs here (gitignored)
├── output/                 # Generated EPUBs land here (gitignored)
├── script_converter.py     # Main converter
├── test_script_converter.py # 46 pytest tests
├── AGENTS.md               # Technical project notes
└── venv/                   # Python virtualenv (gitignored)
```

## Setup

### 1. Install dependencies

```bash
python3 -m venv venv
./venv/bin/pip install PyMuPDF Pillow pytesseract epubcheck pytest
```

OCR fallback (used only for pages without a text layer) additionally needs Tesseract on the system:

```bash
brew install tesseract
```

### 2. Set your API key

The converter reads the key **only** from the `DEEPSEEK_API_KEY` environment variable — there is no CLI flag and no hardcoded key.

```bash
export DEEPSEEK_API_KEY="sk-..."          # add to your shell profile
# or, per-session:
DEEPSEEK_API_KEY="sk-..." ./venv/bin/python script_converter.py ...
```

If the variable is not set, the script exits with an error.

## Usage

```bash
# Basic conversion
./venv/bin/python script_converter.py input/book.pdf

# Explicit output path (defaults to <input>.epub next to the PDF)
./venv/bin/python script_converter.py input/book.pdf -o output/book.epub

# Override metadata (useful for scans with no embedded title/author)
./venv/bin/python script_converter.py input/book.pdf \
    --title "The Historian's Craft" --author "Marc Bloch"
```

### All options

| Flag | Default | Description |
|---|---|---|
| `-o, --output` | `<input>.epub` | Output EPUB path |
| `--no-ocr` | off | Disable OCR fallback (native text only) |
| `--force-ocr` | off | Force OCR on all pages, ignore native text |
| `--include-images` | off | Embed figures/illustrations (off by default to avoid full-page scans) |
| `--no-validate` | off | Skip epubcheck validation |
| `--llm-model` | `deepseek-chat` | DeepSeek model to use |
| `--llm-workers` | `48` | Parallel workers for chunk API calls |
| `--llm-max-iterations` | `3` | Max refinement iterations for LLM output |
| `--title` | — | Override book title in EPUB metadata |
| `--author` | — | Override book author in EPUB metadata |

## How it works

1. Extract PDF metadata + table of contents with PyMuPDF.
2. **Quick text-layer scan**: sample up to 5 pages (first, last, evenly spaced) with the same heuristic the real extraction uses, and report whether OCR is likely to be needed.
3. Extract text page by page (native text layer, or OCR fallback via Tesseract; progress every 100 pages).
4. Split the book into ~50K-character chunks so the LLM has enough output budget to reproduce content faithfully instead of summarizing.
5. Send **all chunks in parallel** (ThreadPoolExecutor, 48 workers) to the DeepSeek API — a book split into N chunks finishes in roughly the time of the slowest chunk.
6. Each chunk returns formatted XHTML chapters as JSON.
7. Reassemble chapters in original page order.
8. Sanitize XHTML (`&amp;` escaping, `<br/>` fixes).
9. Build the EPUB and validate it with epubcheck.
10. Refinement loop (up to 3 iterations) if quality or validation issues are found.

All diagnostics stream live line-by-line, so progress is visible in the terminal or in a redirected log file while the run is in progress.

### Metadata resolution

Book title/author resolution order (highest wins):

1. `--title` / `--author` CLI flags
2. PDF embedded metadata
3. Title/author the LLM reports for chunk 1 (the title page) — placeholder values like "Untitled"/"Unknown" are ignored
4. Placeholders `"Untitled"` / `"Unknown"`

This means scanned books without embedded metadata automatically get their real title and author from the LLM.

### LLM output format

Each chunk is asked to return JSON with XHTML content strings:

```json
{
  "title": "Book Title",
  "author": "Author",
  "chapters": [
    {
      "title": "Chapter Name",
      "content": "Valid XHTML with properly closed tags only"
    }
  ]
}
```

The parser also handles an XML+CDATA format as a fallback for refinement iterations.

## Design decisions

- **LLM-only**: The LLM produces far better formatting (proper paragraphs, chapter detection, OCR fixes) than heuristics ever could. No fallback path — the old deterministic/heuristic conversion path was removed entirely.
- **Env-var-only API key**: The key comes exclusively from `DEEPSEEK_API_KEY` — never hardcoded, never on the command line.
- **Headers/footers handled by the prompt**: the LLM removes running heads, footers and page numbers itself, so it receives the full raw page text.
- **No deduplication**: chapters from different chunks are concatenated, not deduplicated (safer for books with similar section names).

## Tests

```bash
./venv/bin/python -m pytest test_script_converter.py -v
```

46 tests, all passing.

## Known limitations

- DeepSeek output token limits (~8K–32K) mean very long single calls may summarize — solved by chunking.
- macOS SSL certificate issues: the script falls back to an unverified SSL context if certificate verification fails.
- OCR requires `tesseract` installed on the system (`brew install tesseract`).
