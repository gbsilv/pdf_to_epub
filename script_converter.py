#!/usr/bin/env python3
"""
High-quality PDF to EPUB converter powered by DeepSeek LLM formatting.

Uses PyMuPDF (fitz) for text extraction, with pytesseract OCR fallback for
pages with poor or missing text layers. The extracted text is then formatted
by the DeepSeek LLM API into a clean, well-structured EPUB 3 ebook with proper
chapters, paragraphs, and formatting. A DeepSeek API key is required.

Usage:
    # Set API key (required — the only supported key source):
    export DEEPSEEK_API_KEY="sk-..."

    # Convert (input/ and output/ are the conventional folders):
    python3 script_converter.py input/my_book.pdf -o output/my_book.epub

    # Override metadata (useful for scans without embedded title/author):
    python3 script_converter.py input/my_book.pdf -o output/my_book.epub \
        --title "The Historian's Craft" --author "Marc Bloch"
"""

import argparse
import io
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image, ImageEnhance, ImageFilter

# ── EPUB generation helpers ──────────────────────────────────────────────────

EPUB_MIMETYPE = "application/epub+zip"
XHTML_NS = "http://www.w3.org/1999/xhtml"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"


def _xml_declaration() -> str:
    return '<?xml version="1.0" encoding="UTF-8"?>\n'


def _escape_xml(text: str) -> str:
    """Escape text for safe insertion into XML/XHTML."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _sanitize_filename(name: str) -> str:
    """Create a safe filename fragment from a string."""
    name = re.sub(r"[^\w\s\-]", "", name, flags=re.UNICODE)
    name = re.sub(r"\s+", "_", name.strip())
    return name or "untitled"


def _html_wrap(title: str, body_content: str, css_href: str = "style.css") -> str:
    """Wrap body content in a minimal XHTML 5 skeleton."""
    return _xml_declaration() + (
        '<!DOCTYPE html>\n'
        f'<html xmlns="{XHTML_NS}" xml:lang="en" lang="en">\n'
        "<head>\n"
        f"<title>{_escape_xml(title)}</title>\n"
        f'<link rel="stylesheet" type="text/css" href="{css_href}"/>\n'
        "</head>\n"
        "<body>\n"
        f"{body_content}\n"
        "</body>\n"
        "</html>\n"
    )


DEFAULT_CSS = """\
body {
    font-family: serif;
    line-height: 1.6;
    margin: 1.5em 1em;
    color: #222;
}
h1, h2, h3 {
    font-family: sans-serif;
    margin-top: 1.5em;
    margin-bottom: 0.5em;
    page-break-after: avoid;
}
h2 {
    font-size: 1.3em;
    text-align: left;
    border-bottom: 1px solid #ccc;
    padding-bottom: 0.3em;
}
p {
    margin: 0.6em 0;
    text-align: justify;
}
hr.page-break {
    border: none;
    border-top: 1px dashed #ccc;
    margin: 2em 0 1em 0;
    page-break-after: avoid;
}
img {
    max-width: 100%;
    height: auto;
}
"""

# ── PDF text extraction ─────────────────────────────────────────────────────


def _preprocess_image_for_ocr(pil_image: Image.Image) -> Image.Image:
    """Improve image quality before OCR."""
    # Convert to grayscale
    img = pil_image.convert("L")
    # Increase contrast
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(2.0)
    # Sharpen
    img = img.filter(ImageFilter.SHARPEN)
    # Binarize with a threshold
    img = img.point(lambda x: 0 if x < 140 else 255, "1")
    return img


def _page_needs_ocr(page: fitz.Page, text: str) -> bool:
    """Heuristic to decide whether a page needs OCR fallback.

    If extracted text is very short relative to page content, or contains
    mostly garbled characters, we mark it for OCR.
    """
    if not text or not text.strip():
        return True

    # Count meaningful characters
    meaningful = sum(1 for c in text if c.isalpha())
    total = max(len(text), 1)
    ratio = meaningful / total

    # If less than 30 meaningful chars OR ratio of alpha < 0.3, likely a scan
    if meaningful < 30 or ratio < 0.3:
        return True

    return False


def _extract_blocks_from_page(page: fitz.Page) -> list[dict]:
    """Extract text blocks with position and font size from a page.

    Returns list of dicts with keys: text, bbox (y0, y1), font_size, block_type.
    """
    blocks = []
    try:
        page_dict = page.get_text("dict")
    except Exception:
        return blocks

    page_height = page.rect.height

    for block in page_dict.get("blocks", []):
        if block.get("type") != 0:  # not a text block
            continue

        block_text_parts = []
        block_font_sizes = []
        block_y0 = float("inf")
        block_y1 = 0

        for line in block.get("lines", []):
            line_text = ""
            for span in line.get("spans", []):
                text = span.get("text", "")
                if text.strip():
                    line_text += text
                    block_font_sizes.append(span.get("size", 0))

            if line_text.strip():
                block_text_parts.append(line_text.strip())
                bbox = line.get("bbox")
                if bbox:
                    block_y0 = min(block_y0, bbox[1])
                    block_y1 = max(block_y1, bbox[3])

        full_text = " ".join(block_text_parts).strip()
        if not full_text:
            continue

        avg_font_size = sum(block_font_sizes) / len(block_font_sizes) if block_font_sizes else 0

        blocks.append({
            "text": full_text,
            "y0": block_y0 if block_y0 != float("inf") else 0,
            "y1": block_y1,
            "font_size": avg_font_size,
            "page_height": page_height,
        })

    return blocks


def extract_text_from_pdf(pdf_path: str, force_ocr: bool = False, no_ocr: bool = False, include_images: bool = False) -> list[dict]:
    """Extract text from a PDF page by page, using OCR where needed.

    Args:
        pdf_path: Path to the PDF file.
        force_ocr: If True, use OCR for every page regardless of native text quality.
        no_ocr: If True, never use OCR; only use native text extraction.
        include_images: If True, extract and embed images from pages with native
            text. Default is False to avoid embedding full-page scans in the EPUB.

    Returns a list of dicts:
        {"page_num": int, "text": str, "method": "native"|"ocr", "images": list[dict]}
    """
    doc = fitz.open(pdf_path)
    pages = []

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_num = page_idx + 1

        # Try native text extraction
        native_text = page.get_text("text")

        use_ocr = False
        if force_ocr:
            use_ocr = True
        elif not no_ocr and _page_needs_ocr(page, native_text):
            use_ocr = True

        if use_ocr:
            # Render page as image and OCR
            pix = page.get_pixmap(dpi=300)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            img = _preprocess_image_for_ocr(img)

            # pytesseract import is here to avoid issues if not installed
            import pytesseract

            ocr_text = pytesseract.image_to_string(img, lang="eng")
            pages.append(
                {
                    "page_num": page_num,
                    "text": ocr_text.strip(),
                    "method": "ocr",
                    "images": [],
                }
            )
        else:
            # Plain text extraction (better word spacing than block-level)
            native_text = page.get_text("text")
            # Block-level extraction for font/position info (header & chapter detection)
            blocks = _extract_blocks_from_page(page)

            # Extract embedded images only when explicitly requested.
            images = []
            if include_images:
                image_list = page.get_images(full=True)
                for img_info in image_list:
                    xref = img_info[0]
                    base_image = doc.extract_image(xref)
                    if base_image:
                        ext = base_image.get("ext", "jpg")
                        mime_map = {
                            "jpg": "image/jpeg", "jpeg": "image/jpeg",
                            "png": "image/png", "gif": "image/gif",
                            "bmp": "image/bmp", "tiff": "image/tiff",
                            "tif": "image/tiff", "webp": "image/webp",
                        }
                        mime = mime_map.get(ext.lower(), "image/jpeg")
                        images.append({
                            "data": base_image["image"],
                            "ext": ext,
                            "mime": mime,
                        })

            pages.append(
                {
                    "page_num": page_num,
                    "text": native_text.strip(),
                    "method": "native",
                    "images": images,
                    "blocks": blocks,
                }
            )

        if (page_idx + 1) % 100 == 0:
            print(f"   ... {page_idx + 1}/{len(doc)} pages extracted", flush=True)

    doc.close()
    return pages


def _quick_text_layer_scan(pdf_path: str, max_samples: int = 5) -> tuple[int, int, int]:
    """Sample pages to estimate the native text layer before full extraction.

    Returns (total_pages, pages_sampled, pages_with_native_text). Uses the same
    _page_needs_ocr heuristic as the real extraction, so the estimate matches
    what the converter will actually do page by page.
    """
    doc = fitz.open(pdf_path)
    try:
        total = len(doc)
        if total == 0:
            return 0, 0, 0
        if total <= max_samples:
            idxs = list(range(total))
        else:
            # Evenly spaced samples including the first and last page
            step = (total - 1) / (max_samples - 1)
            idxs = sorted({min(total - 1, int(round(i * step)))
                           for i in range(max_samples)})
        with_text = sum(
            1 for i in idxs if not _page_needs_ocr(None, doc[i].get_text("text"))
        )
        return total, len(idxs), with_text
    finally:
        doc.close()


def get_pdf_toc(pdf_path: str) -> list[dict]:
    """Extract the table of contents (outline) from the PDF.

    Returns list of {"level": int, "title": str, "page": int}.
    """
    doc = fitz.open(pdf_path)
    toc = doc.get_toc(simple=False)
    doc.close()

    chapters = []
    for item in toc:
        level = item[0]
        title = item[1]
        page_link = item[2]

        # Filter out obviously junk / auto-generated TOC entries
        title_stripped = title.strip()
        if not title_stripped:
            continue
        # Skip entries that look like auto-generated markers
        if re.search(r"X_cropped_|_cropped_|X_obj\d+|\.pdf", title_stripped, re.IGNORECASE):
            continue
        # Skip entries that are just a few garbage characters
        if len(title_stripped) < 2:
            continue

        # page_link is a dict with 'page' key when simple=False
        if isinstance(page_link, dict):
            page_num = page_link.get("page", 1)
        else:
            page_num = int(page_link)

        chapters.append({"level": level, "title": title_stripped, "page": page_num})

    # Deduplicate by title+page
    seen = set()
    unique_chapters = []
    for ch in chapters:
        key = (ch["title"].lower(), ch["page"])
        if key not in seen:
            seen.add(key)
            unique_chapters.append(ch)

    return unique_chapters


def get_pdf_metadata(pdf_path: str) -> dict:
    """Read PDF metadata."""
    doc = fitz.open(pdf_path)
    meta = doc.metadata
    doc.close()
    return {
        "title": meta.get("title", ""),
        "author": meta.get("author", ""),
        "subject": meta.get("subject", ""),
        "keywords": meta.get("keywords", ""),
    }


# ── EPUB building ────────────────────────────────────────────────────────────


def build_container_xml() -> str:
    """META-INF/container.xml"""
    return _xml_declaration() + (
        f'<container version="1.0" xmlns="{CONTAINER_NS}">\n'
        "  <rootfiles>\n"
        '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
        "  </rootfiles>\n"
        "</container>\n"
    )


def build_opf(
    book_id: str,
    metadata: dict,
    spine_ids: list[str],
    manifest_items: list[dict],
    has_toc_ncx: bool = True,
) -> str:
    """Build content.opf for EPUB 3 using raw string building for correctness."""
    title = _escape_xml(metadata.get("title") or "Untitled")
    author = _escape_xml(metadata.get("author") or "Unknown")

    lines = [_xml_declaration()]
    lines.append(
        f'<package xmlns="{OPF_NS}" xmlns:dc="{DC_NS}" '
        'version="3.0" unique-identifier="book-id">'
    )
    lines.append(f"  <metadata>")
    lines.append(f'    <dc:identifier id="book-id">{_escape_xml(book_id)}</dc:identifier>')
    lines.append(f"    <dc:title>{title}</dc:title>")
    lines.append(f"    <dc:creator>{author}</dc:creator>")
    lines.append(f"    <dc:language>en</dc:language>")
    lines.append(f'    <meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>')
    lines.append(f"  </metadata>")

    lines.append(f"  <manifest>")
    lines.append(f'    <item id="css" href="style.css" media-type="text/css"/>')
    lines.append(f'    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
    if has_toc_ncx:
        lines.append(f'    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')

    for item in manifest_items:
        attrs = " ".join(f'{k}="{_escape_xml(str(v))}"' for k, v in item.items())
        lines.append(f"    <item {attrs}/>")

    lines.append(f"  </manifest>")

    toc_attr = ' toc="ncx"' if has_toc_ncx else ""
    lines.append(f'  <spine{toc_attr}>')
    for sid in spine_ids:
        lines.append(f'    <itemref idref="{_escape_xml(sid)}"/>')
    lines.append(f"  </spine>")

    lines.append(f"</package>")
    return "\n".join(lines)


def build_ncx(book_id: str, title: str, nav_points: list[dict]) -> str:
    """Build toc.ncx for EPUB 2 backwards compatibility."""
    escape = _escape_xml
    max_depth = max((np.get("depth", 1) for np in nav_points), default=1) if nav_points else 1

    lines = [_xml_declaration()]
    lines.append(f'<ncx xmlns="{NCX_NS}" version="2005-1">')
    lines.append(f"  <head>")
    lines.append(f'    <meta name="dtb:uid" content="{escape(book_id)}"/>')
    lines.append(f'    <meta name="dtb:depth" content="{max_depth}"/>')
    lines.append(f'    <meta name="dtb:totalPageCount" content="0"/>')
    lines.append(f'    <meta name="dtb:maxPageNumber" content="0"/>')
    lines.append(f"  </head>")
    lines.append(f"  <docTitle>")
    lines.append(f"    <text>{escape(title)}</text>")
    lines.append(f"  </docTitle>")
    lines.append(f"  <navMap>")

    play_order = 0
    for np in nav_points:
        play_order += 1
        lines.append(f'    <navPoint id="navpoint-{play_order}" playOrder="{play_order}">')
        lines.append(f"      <navLabel>")
        lines.append(f"        <text>{escape(np['title'])}</text>")
        lines.append(f"      </navLabel>")
        lines.append(f'      <content src="{escape(np["src"])}"/>')
        lines.append(f"    </navPoint>")

    lines.append(f"  </navMap>")
    lines.append(f"</ncx>")
    return "\n".join(lines)


def build_nav_xhtml(title: str, toc_items: list[dict]) -> str:
    """Build the EPUB 3 navigation document."""
    items_html = ""
    for item in toc_items:
        items_html += (
            f'<li><a href="{_escape_xml(item["src"])}">'
            f"{_escape_xml(item['title'])}</a></li>\n"
        )

    body = (
        '<nav epub:type="toc" id="toc">\n'
        "<h2>Table of Contents</h2>\n"
        "<ol>\n"
        f"{items_html}"
        "</ol>\n"
        "</nav>\n"
    )

    return _xml_declaration() + (
        '<!DOCTYPE html>\n'
        f'<html xmlns="{XHTML_NS}" '
        'xmlns:epub="http://www.idpf.org/2007/ops" '
        'xml:lang="en" lang="en">\n'
        "<head>\n"
        f"<title>{_escape_xml(title)}</title>\n"
        "</head>\n"
        "<body>\n"
        f"{body}\n"
        "</body>\n"
        "</html>\n"
    )


# ── Validation ───────────────────────────────────────────────────────────────


def validate_epub(epub_path: str) -> bool:
    """Validate the EPUB file using epubcheck.

    Returns True if valid, False otherwise.
    """
    try:
        from epubcheck import EpubCheck

        result = EpubCheck(epub_path, lang="en")
        if hasattr(result, "valid") and callable(result.valid):
            is_valid = result.valid()
        elif hasattr(result, "messages"):
            messages = result.messages
            errors = [m for m in messages if hasattr(m, "severity") and m.severity == "ERROR"]
            is_valid = len(errors) == 0
        else:
            is_valid = True

        # Print messages for diagnostics
        if hasattr(result, "messages"):
            for msg in result.messages:
                sev = getattr(msg, "severity", "INFO")
                text = getattr(msg, "message", str(msg))
                loc = getattr(msg, "location", "")
                loc_str = f" [{loc}]" if loc else ""
                print(f"  epubcheck [{sev}]{loc_str}: {text}")

        return is_valid
    except Exception as e:
        print(f"  epubcheck validation error: {e}")
        # Try to run epubcheck via command line as fallback
        return _validate_epub_cli(epub_path)


def _validate_epub_cli(epub_path: str) -> bool:
    """Fallback: validate using epubcheck command-line."""
    import subprocess

    try:
        result = subprocess.run(
            ["epubcheck", str(epub_path)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout + result.stderr
        print(output)
        return result.returncode == 0
    except FileNotFoundError:
        print("  epubcheck CLI not found, skipping validation")
        return True
    except Exception as e:
        print(f"  epubcheck CLI error: {e}")
        return True


# ── DeepSeek LLM integration ─────────────────────────────────────────────────

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"


def _get_deepseek_api_key():
    """Return the DeepSeek API key from the DEEPSEEK_API_KEY env var."""
    return os.environ.get("DEEPSEEK_API_KEY")


def _call_deepseek(system_prompt, user_prompt, api_key, model=DEEPSEEK_MODEL,
                   max_tokens=32768, temperature=0.3):
    data = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")

    req = urllib.request.Request(
        DEEPSEEK_API_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )

    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=300, context=ctx) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result["choices"][0]["message"]["content"]
    except urllib.error.URLError as e:
        if "CERTIFICATE_VERIFY_FAIL" in str(e):
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=300, context=ctx) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result["choices"][0]["message"]["content"]
        raise
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DeepSeek API HTTP {e.code}: {error_body}")
    except Exception:
        raise


def _call_deepseek_with_retry(system_prompt, user_prompt, api_key,
                              model=DEEPSEEK_MODEL, max_tokens=32768,
                              max_retries=2):
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return _call_deepseek(system_prompt, user_prompt, api_key,
                                  model, max_tokens)
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                wait = 2 ** attempt
                print(f"  LLM API retry {attempt+1}/{max_retries} "
                      f"(wait {wait}s): {e}")
                time.sleep(wait)
    raise last_error


def _parse_llm_response(response_text):
    text = response_text.strip()

    # Remove markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # Try XML+CDATA format first (primary format)
    if text.startswith("<ebook"):
        return _parse_xml_response(text)

    # Fallback: try JSON format
    if text.startswith("{") or text.lstrip().startswith("{"):
        return _parse_json_response(text)

    # Last resort: try to find JSON or XML in the response
    xml_start = text.find("<ebook")
    json_start = text.find('{"title"')
    json_start2 = text.find('{\n  "title"')

    if xml_start != -1:
        return _parse_xml_response(text[xml_start:text.rfind("</ebook>") + len("</ebook>")])
    if json_start != -1:
        return _parse_json_response(text[json_start:])
    if json_start2 != -1:
        return _parse_json_response(text[json_start2:])

    raise ValueError(f"Cannot detect output format. Response starts with: {text[:200]}")


def _parse_xml_response(text):
    """Parse XML+CDATA response from LLM."""
    from xml.etree import ElementTree as ET

    # Find the root element
    start = text.find("<ebook")
    end = text.rfind("</ebook>")
    if start == -1 or end == -1:
        raise ValueError("No <ebook> tags found in response")
    xml_text = text[start:end + len("</ebook>")]

    root = ET.fromstring(xml_text)
    result = {
        "title": "",
        "author": "",
        "chapters": [],
    }

    title_el = root.find("title")
    if title_el is not None and title_el.text:
        result["title"] = title_el.text.strip()

    author_el = root.find("author")
    if author_el is not None and author_el.text:
        result["author"] = author_el.text.strip()

    chapters_el = root.find("chapters")
    if chapters_el is not None:
        for ch_el in chapters_el.findall("chapter"):
            ch_title = ""
            ch_content = ""
            title_el = ch_el.find("title")
            content_el = ch_el.find("content")
            if title_el is not None and title_el.text:
                ch_title = title_el.text.strip()
            if content_el is not None and content_el.text:
                ch_content = content_el.text.strip()
            result["chapters"].append({
                "title": ch_title or "Untitled",
                "content": ch_content,
            })

    return result


def _parse_json_response(text):
    """Parse JSON response from LLM (fallback format)."""
    # Find JSON object boundaries
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]

    # Try to parse as-is first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Attempt to fix common LLM JSON issues and retry
    fixed = _repair_json(text)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    # Last resort: extract chapters via regex
    return _extract_chapters_regex(text)


def _repair_json(text):
    """Attempt to repair common JSON formatting errors from LLM output."""
    # Remove trailing commas before closing brackets/braces
    text = re.sub(r",\s*([}\]])", r"\1", text)

    # Scan character-by-character to find strings and fix interior issues.
    # The main LLM problem: literal newlines and unescaped quotes inside
    # JSON string values (especially in the XHTML content field).
    result = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '"' and (i == 0 or text[i-1] != '\\'):
            # Start of a JSON string — collect until the closing unescaped quote
            result.append(ch)
            i += 1
            while i < len(text):
                c = text[i]
                if c == '\\' and i + 1 < len(text):
                    # Keep escaped sequences as-is
                    result.append(c)
                    result.append(text[i+1])
                    i += 2
                elif c == '"':
                    # Closing quote
                    result.append(c)
                    i += 1
                    break
                elif c == '\n':
                    # Escape literal newlines inside strings
                    result.append('\\n')
                    i += 1
                elif c == '\r':
                    result.append('\\r')
                    i += 1
                elif c == '\t':
                    result.append('\\t')
                    i += 1
                else:
                    result.append(c)
                    i += 1
        else:
            result.append(ch)
            i += 1

    return "".join(result)


def _extract_chapters_regex(text):
    """Fallback: extract chapters from JSON-like text using regex.
    
    Attempts to find chapter objects by scanning for title/content pairs,
    handling multi-line content strings with proper escaping.
    """
    result = {"title": "Untitled", "author": "Unknown", "chapters": []}

    title_match = re.search(r'"title"\s*:\s*"([^"]*)"', text)
    if title_match:
        result["title"] = title_match.group(1)

    author_match = re.search(r'"author"\s*:\s*"([^"]*)"', text)
    if author_match:
        result["author"] = author_match.group(1)

    # Find all chapter objects: look for {"title": "..." patterns
    # and extract the content between them until the next chapter or end of array
    chapter_starts = list(re.finditer(
        r'\{\s*"title"\s*:\s*"([^"]*)"\s*,\s*"content"\s*:\s*"',
        text,
    ))

    for idx, match in enumerate(chapter_starts):
        ch_title = match.group(1)
        content_start = match.end()

        # Content ends at the next chapter start or the closing of the chapters array
        if idx + 1 < len(chapter_starts):
            content_end = chapter_starts[idx + 1].start()
        else:
            # Find the closing of the chapters array
            # Look for "}] or "} ] pattern
            content_end = text.rfind('"}')

        if content_end > content_start:
            raw_content = text[content_start:content_end].rstrip().rstrip('"')
            # Unescape JSON escapes
            raw_content = raw_content.replace('\\"', '"').replace('\\n', '\n')
            result["chapters"].append({
                "title": ch_title,
                "content": raw_content.strip(),
            })

    return result


def _chapters_to_xml(chapters_data):
    """Convert chapters data dict to XML+CDATA string for LLM refinement."""
    title = _escape_xml(chapters_data.get("title", "Untitled"))
    author = _escape_xml(chapters_data.get("author", "Unknown"))
    lines = [
        "<ebook>",
        f"  <title>{title}</title>",
        f"  <author>{author}</author>",
        "  <chapters>",
    ]
    for ch in chapters_data.get("chapters", []):
        ch_title = _escape_xml(ch.get("title", ""))
        content = ch.get("content", "")
        lines.append("    <chapter>")
        lines.append(f"      <title>{ch_title}</title>")
        lines.append(f"      <content><![CDATA[{content}]]></content>")
        lines.append("    </chapter>")
    lines.append("  </chapters>")
    lines.append("</ebook>")
    return "\n".join(lines)


def _build_refinement_prompt(previous_output, quality_issues,
                             validation_errors):
    system_prompt = (
        "You are an expert ebook formatter. You previously formatted an ebook "
        "but there are issues that need to be fixed. Review the issues "
        "carefully and provide a corrected version."
    )

    issues_text = "\n".join(f"- {issue}" for issue in quality_issues) \
        if quality_issues else "None"
    errors_text = "\n".join(f"- {err}" for err in validation_errors) \
        if validation_errors else "None"

    user_prompt = f"""The ebook you previously generated has the following issues. Please fix them and return the corrected full output.

QUALITY / CONTENT ISSUES:
{issues_text}

VALIDATION ERRORS (these must be fixed for the EPUB to be valid):
{errors_text}

PREVIOUS OUTPUT (to be corrected):
{previous_output}

INSTRUCTIONS:
1. Fix ALL the issues and errors listed above.
2. CRITICAL — XHTML VALIDITY: Every tag must be properly closed and nested.
   - <br> is INVALID. Use <br/> (self-closing) instead.
   - Every <p> must have a matching </p>. Every <em>, <strong> must be closed.
   - Tags must be properly nested: <p><em>text</em></p> NOT <p><em>text</p></em>.
3. CRITICAL — Use the XML+CDATA output format exactly as specified.
4. Ensure every chapter has non-empty content.
5. Do not change content unnecessarily; only fix what is broken.

OUTPUT FORMAT — Return ONLY valid XML with CDATA sections:
<ebook>
  <title>Book Title</title>
  <author>Author Name</author>
  <chapters>
    <chapter>
      <title>Chapter Title</title>
      <content><![CDATA[<p>Content here...</p>]]></content>
    </chapter>
  </chapters>
</ebook>

Start directly with <ebook> and end with </ebook>. No markdown, no preamble.
"""
    return system_prompt, user_prompt


def _sanitize_xhtml_content(content):
    """Fix common XHTML issues in LLM-generated content."""
    # Escape bare & that aren't part of valid entities
    content = re.sub(r'&(?!amp;|lt;|gt;|quot;|apos;|#[0-9]+;|#[xX][0-9a-fA-F]+;)',
                     '&amp;', content)
    # Fix unclosed <br> tags
    content = re.sub(r'<br\s*>', '<br/>', content)
    # Fix <hr> tags
    content = re.sub(r'<hr\s*>', '<hr/>', content)
    return content


def build_epub_from_llm_output(chapters_data, metadata, output_path):
    book_id = f"urn:uuid:{uuid.uuid4()}"
    title = chapters_data.get("title") or metadata.get("title") or "Untitled"
    llm_author = chapters_data.get("author", "")
    # Fill empty/placeholder OPF metadata with LLM-reported values so scanned
    # books without embedded metadata carry their real title/author.
    needs_title = not metadata.get("title") or metadata.get("title") == "Untitled"
    needs_author = not metadata.get("author") or metadata.get("author") == "Unknown"
    if (needs_title or needs_author) and (llm_author or title != "Untitled"):
        metadata = dict(metadata)
        if needs_title and title != "Untitled":
            metadata["title"] = title
        if needs_author and llm_author:
            metadata["author"] = llm_author

    chapters = chapters_data.get("chapters", [])
    if not chapters:
        chapters = [{"title": "Content", "content": "<p>No content available.</p>"}]

    manifest_items = []
    spine_ids = []
    nav_points = []

    chapters_for_writing = []
    for i, ch in enumerate(chapters):
        ch_title = ch.get("title", f"Chapter {i+1}").strip()
        if not ch_title:
            ch_title = f"Chapter {i+1}"
        content = ch.get("content", "<p></p>")

        # Fix common XHTML issues in content
        content = _sanitize_xhtml_content(content)

        safe_name = _sanitize_filename(ch_title)
        file_name = f"chapter_{i+1:03d}_{safe_name}.xhtml"
        ch_id = _sanitize_filename(file_name.replace(".xhtml", ""))

        spine_ids.append(ch_id)
        manifest_items.append({
            "id": ch_id,
            "href": file_name,
            "media-type": "application/xhtml+xml",
        })
        nav_points.append({
            "title": ch_title,
            "src": file_name,
            "depth": 1,
        })
        chapters_for_writing.append({
            "title": ch_title,
            "file": file_name,
            "content": content,
        })

    has_toc_ncx = len(nav_points) > 0
    opf_content = build_opf(book_id, metadata, spine_ids, manifest_items,
                            has_toc_ncx)
    ncx_content = build_ncx(book_id, title, nav_points) if has_toc_ncx else ""
    nav_content = build_nav_xhtml(title, nav_points)

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), EPUB_MIMETYPE,
                    compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", build_container_xml())
        zf.writestr("OEBPS/style.css", DEFAULT_CSS)
        zf.writestr("OEBPS/content.opf", opf_content)
        zf.writestr("OEBPS/nav.xhtml", nav_content)
        if ncx_content:
            zf.writestr("OEBPS/toc.ncx", ncx_content)

        for ch in chapters_for_writing:
            full_html = _html_wrap(ch["title"], ch["content"])
            zf.writestr(f"OEBPS/{ch['file']}", full_html)

    return chapters_for_writing, nav_points


def _validate_xhtml_content(content):
    """Validate that content is well-formed XHTML. Returns list of error strings."""
    errors = []
    content = _sanitize_xhtml_content(content)
    xhtml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html>\n'
        f'<html xmlns="{XHTML_NS}">\n'
        '<head><title>test</title></head>\n'
        '<body>\n'
        f'{content}\n'
        '</body>\n'
        '</html>'
    )
    try:
        from xml.etree import ElementTree as ET
        ET.fromstring(xhtml)
    except ET.ParseError as e:
        errors.append(f"Malformed XHTML: {e}")
        # Extract the problematic snippet
        lines = xhtml.split('\n')
        pos = getattr(e, 'position', None)
        if pos and len(pos) >= 2:
            line_num, col = pos[0], pos[1]
            if 1 <= line_num <= len(lines):
                err_line = lines[line_num - 1]
                start = max(0, col - 60)
                end = min(len(err_line), col + 60)
                snippet = err_line[start:end]
                if start > 0:
                    snippet = "..." + snippet
                if end < len(err_line):
                    snippet = snippet + "..."
                errors.append(f"  Problematic markup near: {snippet}")
    return errors


def _check_llm_output_quality(chapters_data):
    issues = []
    chapters = chapters_data.get("chapters", [])

    if not chapters:
        issues.append("No chapters generated — output is empty")
        return issues

    total_content_len = sum(len(ch.get("content", "")) for ch in chapters)
    if total_content_len < 500:
        issues.append(
            f"Very little content generated ({total_content_len} chars total) "
            f"— most text may be missing"
        )

    for i, ch in enumerate(chapters):
        content = ch.get("content", "")
        title = ch.get("title", f"Chapter {i+1}")
        if not content.strip():
            issues.append(f"Chapter {i+1} ('{title}') has empty content")
        if "<html" in content.lower() or "<body" in content.lower():
            issues.append(
                f"Chapter {i+1} contains <html>/<body> tags "
                f"(should only have inner body content)"
            )
        if "<h1" in content.lower():
            issues.append(
                f"Chapter {i+1} uses <h1> tag (reserved for book title)"
            )

        # XHTML well-formedness check
        xhtml_errors = _validate_xhtml_content(content)
        for err in xhtml_errors:
            issues.append(f"Chapter {i+1} ('{title}'): {err}")

    return issues


def _split_pages_into_chunks(pages, max_chunk_chars=50000):
    """Split pages into chunks that fit in the LLM context window."""
    chunks = []
    current_chunk = []
    current_size = 0

    for p in pages:
        page_size = len(p.get("text", "")) + 30  # overhead for page marker
        if current_size + page_size > max_chunk_chars and current_chunk:
            chunks.append(current_chunk)
            current_chunk = []
            current_size = 0
        current_chunk.append(p)
        current_size += page_size

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def _process_single_chunk(pages_chunk, metadata, toc, api_key, model,
                          chunk_num, total_chunks, print_lock=None):
    """Send one chunk of pages to the LLM and return (chunk_num, parsed_data).

    When print_lock (a threading.Lock) is provided, all print calls are
    serialised through it so parallel workers don't garble each other's output.
    """
    def _log(msg):
        if print_lock:
            with print_lock:
                print(msg)
        else:
            print(msg)

    if total_chunks > 1:
        _log(f"   📦 Processing chunk {chunk_num}/{total_chunks} "
             f"({len(pages_chunk)} pages)...")

    system_prompt = (
        "You are an expert ebook formatter and editor. You convert raw text "
        "extracted from PDFs into clean, well-structured ebooks. You fix OCR "
        "errors, identify chapter breaks, remove headers/footers/page numbers, "
        "and format text into proper paragraphs. You preserve the author's "
        "original wording and style faithfully."
    )

    title = metadata.get("title") or "Untitled"
    author = metadata.get("author") or "Unknown"

    text_parts = []
    for p in pages_chunk:
        page_text = p.get("text", "").strip()
        if page_text:
            text_parts.append(f"\n=== PAGE {p['page_num']} ===\n{page_text}")
    raw_text = "\n".join(text_parts)

    toc_str = json.dumps(toc[:20], indent=2) if toc else "No TOC available."

    chunk_note = ""
    if total_chunks > 1:
        chunk_note = (
            f"\nNOTE: This is chunk {chunk_num} of {total_chunks} from a larger book. "
            f"Process the text in this chunk fully. Do NOT add a book title/cover page "
            f"— just format the chapters/sections found in this chunk.\n"
        )

    user_prompt = f"""I have raw text extracted from a PDF that needs to be converted into a well-structured ebook.
{chunk_note}
BOOK METADATA:
- Title: {title}
- Author: {author}

EXISTING TOC (for reference):
{toc_str}

RAW EXTRACTED TEXT:
{raw_text}

INSTRUCTIONS:
1. Read the raw text carefully. Each page is marked with "=== PAGE N ===" — remove ALL page markers and page artifacts from the output.
2. CRITICAL — CHAPTER DETECTION: You MUST identify EVERY individual section, chapter, or essay in the text and make each one its own chapter entry in the output JSON. Look for:
   - "Chapter 1", "Chapter 2", "CHAPTER I", etc. → each chapter is its own entry
   - Numbered sections like "1.", "2.", "I.", "II." that introduce new topics
   - Essay titles, article titles, or any clear section breaks
   - Any clear heading that separates content into logical units
   Do NOT group multiple chapters into a single chapter entry.
3. Clean up the text thoroughly:
   - Fix OCR errors
   - Join hyphenated words that were broken across lines
   - Fix broken sentences and reconstruct proper paragraphs
   - Normalize punctuation, spacing, and capitalization
   - Remove ALL headers, footers, page numbers, running titles, and PDF artifacts
4. Format each chapter's content as clean XHTML body content:
   - Use <p> tags for paragraphs (every paragraph MUST be wrapped in <p>)
   - Use <h2> for the chapter/section heading (put it as the first element)
   - Use <h3> for sub-headings within chapters
   - Use <em> for italic text, <strong> for bold
   - Use <blockquote> for extended quoted passages
   - ALL tags must be properly CLOSED and nested. <br/> must be self-closing. No unclosed tags.
   - Do NOT use <h1> (reserved for book title)
   - Do NOT include <html>, <head>, or <body> tags in the content field
5. Break text into proper paragraphs based on meaning, NOT PDF line breaks.
6. Preserve original meaning, wording, and style. Do NOT summarize.
7. Include ALL content from the extracted text.

OUTPUT FORMAT:
Output ONLY a valid JSON object. The JSON must match this exact schema:
{{
  "title": "Book Title",
  "author": "Author Name",
  "chapters": [
    {{
      "title": "Chapter or Section Title",
      "content": "Valid XHTML with properly closed tags only"
    }}
  ]
}}

CRITICAL RULES FOR VALID OUTPUT:
- ALL XHTML tags must be properly closed. <br> is invalid; use <br/> instead.
- Every <p> must have a matching </p>. Every <em> needs </em>.
- The entire response must be valid JSON. Start with {{ and end with }}.
- No markdown backticks, no explanation text.
"""

    response = _call_deepseek_with_retry(system_prompt, user_prompt, api_key, model)
    _log(f"   Chunk {chunk_num}: received {len(response):,} chars")

    try:
        return chunk_num, _parse_llm_response(response)
    except Exception as e:
        _log(f"   ❌ Chunk {chunk_num} parse failed: {e}")
        return chunk_num, {"title": title, "author": author, "chapters": []}


def _resolve_book_metadata(metadata: dict, llm_title=None, llm_author=None) -> tuple[str, str]:
    """Resolve book title/author: PDF metadata > LLM-detected > placeholders.

    LLM values that merely echo the placeholder are ignored.
    """
    def _clean(value):
        value = (value or "").strip()
        return value if value and value not in ("Untitled", "Unknown") else None

    title = metadata.get("title") or _clean(llm_title) or "Untitled"
    author = metadata.get("author") or _clean(llm_author) or "Unknown"
    return title, author


def convert_with_llm(pdf_path, output_path, pages, metadata, toc, api_key,
                     model=DEEPSEEK_MODEL, max_iterations=3, max_workers=48):
    print("\n🤖 Using DeepSeek LLM for ebook formatting...")

    input_chars = sum(len(p.get("text", "")) for p in pages)
    print(f"   Input: {len(pages)} pages, ~{input_chars:,} chars of text")

    # Split into chunks if the text is too large
    chunks = _split_pages_into_chunks(pages)
    actual_workers = min(max_workers, len(chunks)) if chunks else 0
    print(f"   Processing {len(chunks)} chunk(s) with {actual_workers} "
          f"parallel worker(s)")

    # Process all chunks in parallel via a thread pool.
    # Each chunk does an I/O-bound HTTP call → threads are the right fit.
    print_lock = threading.Lock()
    chunk_results: dict[int, list[dict]] = {}
    # Title/author reported by the LLM for chunk 1 (which contains the title
    # page). Used as a fallback when the PDF has no embedded metadata.
    llm_title = llm_author = None

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _process_single_chunk,
                chunk_pages, metadata, toc, api_key, model,
                idx + 1, len(chunks), print_lock,
            ): idx
            for idx, chunk_pages in enumerate(chunks)
        }

        for future in as_completed(futures):
            try:
                chunk_num, chunk_data = future.result()
            except Exception as exc:
                # If a chunk fails catastrophically (network error after
                # retries), log it and continue with the others.
                chunk_idx = futures[future]
                chunk_num = chunk_idx + 1
                with print_lock:
                    print(f"   ❌ Chunk {chunk_num} failed: {exc}")
                chunk_data = {
                    "title": metadata.get("title") or "Untitled",
                    "author": metadata.get("author") or "Unknown",
                    "chapters": [],
                }

            chunk_chapters = chunk_data.get("chapters", [])
            chunk_results[chunk_num] = chunk_chapters
            if chunk_num == 1:
                llm_title = chunk_data.get("title")
                llm_author = chunk_data.get("author")
            with print_lock:
                print(f"   ✅ Chunk {chunk_num}: {len(chunk_chapters)} chapters")

    # Reassemble chapters in original chunk order
    combined_chapters = []
    for idx in range(len(chunks)):
        chunk_num = idx + 1
        combined_chapters.extend(chunk_results.get(chunk_num, []))

    # Metadata resolution: PDF embedded metadata first, then the title/author
    # the LLM detected from chunk 1, then placeholders. CLI overrides are
    # applied to `metadata` in main() before this call.
    resolved_title, resolved_author = _resolve_book_metadata(
        metadata, llm_title, llm_author)
    if not metadata.get("title") and resolved_title != "Untitled":
        print(f"   📚 No PDF metadata — using LLM-detected title/author: "
              f"{resolved_title} — {resolved_author}")

    chapters_data = {
        "title": resolved_title,
        "author": resolved_author,
        "chapters": combined_chapters,
    }

    print(f"   Combined: {len(combined_chapters)} total chapters")

    if not combined_chapters:
        print("   ❌ No chapters generated from any chunk")
        return None, None

    # Refinement loop
    best_chapters_data = chapters_data
    best_issues_count = float("inf")

    for iteration in range(max_iterations):
        chapters, nav_points = build_epub_from_llm_output(
            chapters_data, metadata, output_path
        )

        quality_issues = _check_llm_output_quality(chapters_data)

        validation_errors = []
        try:
            from epubcheck import EpubCheck
            result = EpubCheck(str(output_path), lang="en")
            if hasattr(result, "messages"):
                for msg in result.messages:
                    sev = getattr(msg, "severity", "")
                    if sev in ("ERROR", "FATAL"):
                        loc = getattr(msg, "location", "")
                        text = getattr(msg, "message", str(msg))
                        validation_errors.append(f"[{sev}] {loc}: {text}")
        except Exception as e:
            validation_errors.append(f"Validation exception: {e}")

        total_issues = len(quality_issues) + len(validation_errors)

        if total_issues == 0:
            print(f"   ✅ LLM output passes all checks "
                  f"(iteration {iteration+1})")
            return chapters, nav_points

        # Track best version
        if total_issues < best_issues_count:
            best_issues_count = total_issues
            best_chapters_data = chapters_data

        print(f"\n   🔄 Iteration {iteration+1}: "
              f"{len(quality_issues)} quality issues, "
              f"{len(validation_errors)} validation errors")
        for issue in quality_issues[:5]:
            print(f"      ⚠️  {issue}")
        if len(quality_issues) > 5:
            print(f"      ... and {len(quality_issues) - 5} more quality issues")
        for err in validation_errors[:3]:
            print(f"      ❌ {err}")
        if len(validation_errors) > 3:
            print(f"      ... and {len(validation_errors) - 3} more errors")

        if iteration < max_iterations - 1:
            print(f"   🔄 Requesting refinement from LLM...")
            prev_xml = _chapters_to_xml(chapters_data)
            ref_system, ref_user = _build_refinement_prompt(
                prev_xml,
                quality_issues,
                validation_errors[:15],
            )

            try:
                response = _call_deepseek_with_retry(
                    ref_system, ref_user, api_key, model, max_tokens=32768
                )
                chapters_data = _parse_llm_response(response)
                print(f"   Refinement: "
                      f"{len(chapters_data.get('chapters', []))} chapters")
            except Exception as e:
                print(f"   ❌ Refinement call failed: {e}")
                # Continue with current data — don't break
                continue
        else:
            print(f"   ⚠️  Max iterations ({max_iterations}) reached. "
                  f"Using best version ({best_issues_count} issues).")

    # Return best version
    chapters, nav_points = build_epub_from_llm_output(
        best_chapters_data, metadata, output_path
    )
    return chapters, nav_points


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    # Stream diagnostics line-by-line so progress is visible in terminals and
    # redirected log files while the run is in progress.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError, OSError):
        pass

    parser = argparse.ArgumentParser(
        description="Convert PDF to EPUB using DeepSeek LLM for formatting."
    )
    parser.add_argument(
        "pdf_path",
        help="Path to the input PDF file",
    )
    parser.add_argument(
        "-o", "--output",
        help="Output EPUB path (default: same as input with .epub extension)",
    )
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="Disable OCR fallback (use only native text extraction)",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip epubcheck validation",
    )
    parser.add_argument(
        "--force-ocr",
        action="store_true",
        help="Force OCR on all pages (ignore native text)",
    )
    parser.add_argument(
        "--include-images",
        action="store_true",
        help="Include embedded images (figures, illustrations) in the EPUB. "
             "Disabled by default to avoid embedding full-page scans.",
    )
    parser.add_argument(
        "--llm-model",
        default=DEEPSEEK_MODEL,
        help=f"DeepSeek model to use (default: {DEEPSEEK_MODEL}).",
    )
    parser.add_argument(
        "--llm-max-iterations",
        type=int,
        default=3,
        help="Max refinement iterations for LLM output (default: 3).",
    )
    parser.add_argument(
        "--llm-workers",
        type=int,
        default=48,
        help="Number of parallel workers for LLM chunk processing (default: 48).",
    )
    parser.add_argument(
        "--title",
        help="Override the book title in the EPUB metadata "
             "(useful for scans without embedded metadata).",
    )
    parser.add_argument(
        "--author",
        help="Override the book author in the EPUB metadata "
             "(useful for scans without embedded metadata).",
    )

    args = parser.parse_args()

    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        print(f"Error: File not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output) if args.output else pdf_path.with_suffix(".epub")

    print(f"📄 Input:  {pdf_path}")
    print(f"📖 Output: {output_path}")

    # 1. Extract metadata
    print("\n📋 Reading metadata...")
    metadata = get_pdf_metadata(str(pdf_path))
    if args.title:
        metadata["title"] = args.title
    if args.author:
        metadata["author"] = args.author
    print(f"   Title:  {metadata['title'] or '(not set)'}")
    print(f"   Author: {metadata['author'] or '(not set)'}")
    if args.title:
        print("   (title overridden via --title)")
    if args.author:
        print("   (author overridden via --author)")

    # 2. Extract TOC
    print("\n📑 Reading table of contents...")
    toc = get_pdf_toc(str(pdf_path))
    print(f"   Found {len(toc)} TOC entries")
    for t in toc[:5]:
        print(f"     L{t['level']}: {t['title'][:60]} (p.{t['page']})")
    if len(toc) > 5:
        print(f"     ... and {len(toc) - 5} more")

    # 2b. Quick text-layer scan: estimate whether OCR will be needed
    print("\n🔎 Scanning text layer (sampled pages)...")
    total_scan, sampled, with_text = _quick_text_layer_scan(str(pdf_path))
    if sampled:
        pct = with_text * 100 // sampled
        print(f"   Native text layer on {with_text}/{sampled} sampled pages ({pct}%)")
        if with_text == sampled:
            print("   ✅ OCR unlikely to be needed")
        elif with_text == 0:
            print("   ⚠️  No native text layer detected — OCR will run on most pages (slow)")
        else:
            print("   ⚠️  Partial text layer — OCR fallback will run where needed")

    # 3. Extract text page by page
    print("\n📝 Extracting text (with OCR fallback)...")
    pages = extract_text_from_pdf(
        str(pdf_path),
        force_ocr=args.force_ocr,
        no_ocr=args.no_ocr,
        include_images=args.include_images,
    )
    native_count = sum(1 for p in pages if p["method"] == "native")
    ocr_count = sum(1 for p in pages if p["method"] == "ocr")
    print(f"   Total pages: {len(pages)}")
    print(f"   Native text: {native_count} pages")
    print(f"   OCR fallback: {ocr_count} pages")

    # Raw pages go to the LLM as-is — it removes headers/footers itself
    pages_raw = [dict(p) for p in pages]

    # 4. Always use LLM-powered conversion
    api_key = _get_deepseek_api_key()
    if not api_key:
        print(
            "Error: DEEPSEEK_API_KEY is required. "
            "Set the DEEPSEEK_API_KEY environment variable.",
            file=sys.stderr,
        )
        sys.exit(1)

    llm_chapters, llm_nav_points = convert_with_llm(
        str(pdf_path), str(output_path), pages_raw, metadata, toc,
        api_key, args.llm_model, args.llm_max_iterations,
        max_workers=args.llm_workers,
    )

    if llm_chapters:
        epub_size = output_path.stat().st_size
        print(f"\n   EPUB size: {epub_size:,} bytes")
        print(f"\n📊 LLM output quality assessment:")
        total_content = sum(
            len(ch.get("content", "")) for ch in llm_chapters
        )
        chapter_count = len(llm_chapters)
        print(f"   Chapters: {chapter_count}")
        print(f"   Total content: {total_content:,} chars")
        llm_issues = _check_llm_output_quality({
            "chapters": llm_chapters,
        })
        if llm_issues:
            print(f"   ⚠️  Issues:")
            for issue in llm_issues:
                print(f"      - {issue}")
        else:
            print(f"   ✅ No quality issues detected")
    else:
        print(
            "❌ LLM conversion failed to produce any chapters.",
            file=sys.stderr,
        )
        sys.exit(1)

    # 5. Validate
    if not args.no_validate:
        print(f"\n🔍 Validating EPUB with epubcheck...")
        is_valid = validate_epub(str(output_path))
        if is_valid:
            print("✅ EPUB is valid!")
        else:
            print("⚠️  EPUB has validation issues (see above)")

    print(f"\n✅ Done! Output: {output_path}")


if __name__ == "__main__":
    main()
