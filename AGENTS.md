# pdf_to_epub — Project Context

> **⚠️ This file must be kept up-to-date with every code change.** When you modify `script_converter.py` or `test_script_converter.py`, reflect the changes here — updated defaults, new flags, changed behavior, removed features, etc. Treat this as a living document that always matches the current state of the code.

## Overview
Python script that converts PDFs to EPUB 3 using DeepSeek LLM-powered formatting. The LLM path is the **only** conversion path — the API key is always required, read **only** from the `DEEPSEEK_API_KEY` environment variable.

## Folder layout
- `input/` — source PDFs (gitignored)
- `output/` — generated EPUBs (gitignored)
- `script_converter.py`, `test_script_converter.py`, `README.md`, `.gitignore` at repo root

## Key files
- `script_converter.py` — Main converter (1600+ lines; the old heuristic/deterministic path was removed — LLM-only)
- `test_script_converter.py` — 49 pytest tests (all passing)
- `README.md` — User-facing docs

## API key
```
export DEEPSEEK_API_KEY="sk-..."
```
The key comes **only** from the `DEEPSEEK_API_KEY` environment variable — there is no `--deepseek-api-key` flag and no hardcoded key anywhere. Set it in your shell profile. **Required** — the script will exit with an error if the variable is not set.

## Dependencies (all installed in `venv/`)
```
PyMuPDF (fitz)   — PDF reading, text extraction, TOC, metadata
Pillow           — OCR image preprocessing
pytesseract      — OCR fallback for scanned pages
epubcheck        — EPUB 3 structural validation
pytest           — Test framework
```
No `requirements.txt` exists. To install: `pip install PyMuPDF Pillow pytesseract epubcheck pytest`

## How it works

### LLM path (the only path)
1. Extract PDF metadata + TOC via PyMuPDF
2. Quick text-layer scan (`_quick_text_layer_scan`): samples up to 5 pages (first, last, evenly spaced) with the same `_page_needs_ocr` heuristic the real extraction uses, and reports whether OCR is likely to be needed
3. Page-by-page text extraction (native text layer or OCR fallback; progress printed every 100 pages)
4. Split into ~50K char chunks if needed
5. Send ALL chunks in parallel (ThreadPoolExecutor, 48 workers default) to DeepSeek API
6. Each chunk returns formatted XHTML chapters via JSON
7. Reassemble chapters in original order
8. XHTML sanitizing (&amp; escaping, <br/> fixing)
9. Build EPUB and validate with epubcheck
10. Refinement loop (up to 3 iterations) if quality/validation issues found

All diagnostics stream live: `main()` reconfigures stdout to line buffering, so progress is visible in terminals and redirected log files while the run is in progress (no buffered output).

### Metadata resolution
Book title/author resolution order (highest wins):
1. `--title` / `--author` CLI flags
2. PDF embedded metadata (`get_pdf_metadata`)
3. Title/author the LLM reports for chunk 1 (which contains the title page) — captured in `convert_with_llm` and cleaned by `_resolve_book_metadata` (LLM values echoing the "Untitled"/"Unknown" placeholders are ignored)
4. Placeholders "Untitled" / "Unknown"

`build_epub_from_llm_output` fills empty/placeholder OPF metadata with the resolved LLM values, so scanned books without embedded metadata get their real title/author automatically.

## CLI usage

```bash
# LLM conversion (requires API key); EPUB lands in output/ by default
export DEEPSEEK_API_KEY="sk-..."
python3 script_converter.py input/book.pdf

# Custom output path
python3 script_converter.py input/book.pdf -o output/custom-name.epub

# Control parallel workers (default: 48)
python3 script_converter.py input/book.pdf --llm-workers 64

# Skip epubcheck validation
python3 script_converter.py input/book.pdf --no-validate

# Force OCR on all pages
python3 script_converter.py input/book.pdf --force-ocr

# Include embedded images
python3 script_converter.py input/book.pdf --include-images

# Control refinement iterations
python3 script_converter.py input/book.pdf --llm-max-iterations 1

# Custom model
python3 script_converter.py input/book.pdf --llm-model deepseek-chat

# Override metadata (useful for scans with no embedded title/author)
python3 script_converter.py input/book.pdf \
    --title "The Historian's Craft" --author "Marc Bloch"
```

Default output: `output/<pdf name>.epub` (via `_default_output_path` / `DEFAULT_OUTPUT_DIR` — the repo's `output/` folder next to the script). The destination folder is created if missing.

## Parallel chunk processing
Chunks are processed concurrently via `concurrent.futures.ThreadPoolExecutor` (default: 48 workers). Since each chunk makes an I/O-bound HTTP call to the DeepSeek API, threads are the right primitive. A book split into N chunks completes in roughly the time of the single slowest chunk rather than N × chunk_time. Results are reassembled in original page order.

## LLM output format (JSON)
The chunk prompt asks the LLM to output JSON with XHTML content strings:
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
The parser also handles XML+CDATA format as a fallback for refinement iterations.

## Key design decisions
- **LLM-only**: The LLM produces far better formatting (proper paragraphs, chapter detection, OCR fixes) than heuristics ever could. No fallback — if the API key is missing, the script exits with an error.
- **No deterministic path**: The old heuristic conversion path (`build_epub`, `_pages_to_chapters`, `_strip_headers_footers`, `_detect_chapters_from_content`, `check_epub_quality`) was deleted. Header/footer stripping is handled by the LLM prompt, which needs the full raw text.
- **Thread-based parallelism**: Chunk API calls are I/O-bound, so threads avoid the complexity of async while delivering the same throughput.
- **50K char chunks**: Splits long PDFs so the LLM has enough output budget to faithfully reproduce content (not summarize), while keeping individual chunk processing fast.
- **`&` auto-escaping**: Bare `&` in text is escaped to `&amp;` before EPUB writing.
- **No deduplication**: Chapters from different chunks are concatenated, not deduplicated (safer for books with similar section names).

## Known limitations
- DeepSeek output token limit (~8K-32K) means very long single calls may summarize. Solved by chunking.
- macOS SSL cert issue: script falls back to unverified SSL context if cert verification fails.
- OCR requires `tesseract` installed on the system: `brew install tesseract`
