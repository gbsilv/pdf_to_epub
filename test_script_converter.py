"""Tests for script_converter.py — TDD: Happy Path, Edge Cases, Error Cases, Integration."""

import zipfile

import pytest
from PIL import Image

# Import the module under test
import script_converter as sc


# ── _escape_xml ──────────────────────────────────────────────────────────────

class TestEscapeXml:
    """Tests for _escape_xml."""

    # ── Happy Path ──────────────────────────────────────────
    def test_plain_text_unchanged(self):
        assert sc._escape_xml("Hello World") == "Hello World"

    def test_ampersand_escaped(self):
        assert sc._escape_xml("A & B") == "A &amp; B"

    def test_all_special_chars_escaped(self):
        result = sc._escape_xml('<hello attr="val" & \'>')
        assert "&lt;" in result
        assert "&quot;" in result
        assert "&amp;" in result
        assert "&apos;" in result

    # ── Edge Cases ──────────────────────────────────────────
    def test_empty_string(self):
        assert sc._escape_xml("") == ""

    def test_only_special_chars(self):
        result = sc._escape_xml('&<>"\'')
        assert "&amp;" in result
        assert "&lt;" in result
        assert "&gt;" in result

    def test_unicode_text(self):
        assert sc._escape_xml("café – résumé") == "café – résumé"

    def test_already_escaped(self):
        # Double-encoding — the function always encodes
        result = sc._escape_xml("&amp;")
        assert result == "&amp;amp;"


# ── _sanitize_filename ──────────────────────────────────────────────────────

class TestSanitizeFilename:
    """Tests for _sanitize_filename."""

    # ── Happy Path ──────────────────────────────────────────
    def test_simple_name(self):
        assert sc._sanitize_filename("Chapter One") == "Chapter_One"

    def test_multiple_spaces(self):
        assert sc._sanitize_filename("Chapter    One") == "Chapter_One"

    # ── Edge Cases ──────────────────────────────────────────
    def test_empty_string(self):
        assert sc._sanitize_filename("") == "untitled"

    def test_only_special_chars(self):
        assert sc._sanitize_filename("!@#$%") == "untitled"

    def test_with_hyphens(self):
        assert sc._sanitize_filename("Chapter-One") == "Chapter-One"

    def test_unicode_accents(self):
        result = sc._sanitize_filename("café résumé")
        assert result == "café_résumé"


# ── _xml_declaration ─────────────────────────────────────────────────────────

class TestXmlDeclaration:
    """Tests for _xml_declaration."""

    def test_returns_valid_xml_declaration(self):
        result = sc._xml_declaration()
        assert '<?xml version="1.0" encoding="UTF-8"?>' in result


# ── build_container_xml ──────────────────────────────────────────────────────

class TestBuildContainerXml:
    """Tests for build_container_xml."""

    def test_contains_expected_namespace(self):
        result = sc.build_container_xml()
        assert "urn:oasis:names:tc:opendocument:xmlns:container" in result
        assert "OEBPS/content.opf" in result


# ── build_opf ────────────────────────────────────────────────────────────────

class TestBuildOpf:
    """Tests for build_opf."""

    def test_minimal_opf(self):
        result = sc.build_opf(
            book_id="urn:uuid:test-123",
            metadata={"title": "Test Book", "author": "Author Name"},
            spine_ids=["ch1"],
            manifest_items=[
                {"id": "ch1", "href": "chapter_001.xhtml", "media-type": "application/xhtml+xml"}
            ],
        )
        assert "Test Book" in result
        assert "Author Name" in result
        assert "urn:uuid:test-123" in result
        assert 'idref="ch1"' in result

    def test_opf_with_special_chars_in_metadata(self):
        result = sc.build_opf(
            book_id="urn:uuid:test-456",
            metadata={"title": "A & B", "author": 'C "D"'},
            spine_ids=["ch1"],
            manifest_items=[
                {"id": "ch1", "href": "ch.xhtml", "media-type": "application/xhtml+xml"}
            ],
        )
        assert "A &amp; B" in result
        assert "C &quot;D&quot;" in result

    def test_opf_without_ncx(self):
        result = sc.build_opf(
            book_id="urn:uuid:test",
            metadata={"title": "T", "author": "A"},
            spine_ids=["ch1"],
            manifest_items=[{"id": "ch1", "href": "c.xhtml", "media-type": "application/xhtml+xml"}],
            has_toc_ncx=False,
        )
        assert 'toc="ncx"' not in result


# ── build_ncx ────────────────────────────────────────────────────────────────

class TestBuildNcx:
    """Tests for build_ncx."""

    def test_single_nav_point(self):
        result = sc.build_ncx(
            book_id="urn:uuid:test",
            title="My Book",
            nav_points=[{"title": "Chapter 1", "src": "ch1.xhtml", "depth": 1}],
        )
        assert "My Book" in result
        assert "Chapter 1" in result
        assert "ch1.xhtml" in result

    def test_empty_nav_points(self):
        result = sc.build_ncx(
            book_id="urn:uuid:test",
            title="Book",
            nav_points=[],
        )
        assert "Book" in result
        assert "<navMap>" in result


# ── build_nav_xhtml ──────────────────────────────────────────────────────────

class TestBuildNavXhtml:
    """Tests for build_nav_xhtml."""

    def test_with_items(self):
        result = sc.build_nav_xhtml(
            title="TOC",
            toc_items=[{"title": "Intro", "src": "intro.xhtml"}],
        )
        assert "Intro" in result
        assert "intro.xhtml" in result
        assert 'epub:type="toc"' in result

    def test_empty_items(self):
        result = sc.build_nav_xhtml(title="TOC", toc_items=[])
        assert "TOC" in result


# ── _html_wrap ───────────────────────────────────────────────────────────────

class TestHtmlWrap:
    """Tests for _html_wrap."""

    def test_wraps_content(self):
        result = sc._html_wrap("Title", "<p>Hello</p>")
        assert "<title>Title</title>" in result
        assert "<p>Hello</p>" in result
        assert 'xmlns="http://www.w3.org/1999/xhtml"' in result

    def test_special_chars_in_title(self):
        result = sc._html_wrap("A & B", "<p>x</p>")
        assert "<title>A &amp; B</title>" in result


# ── _preprocess_image_for_ocr ────────────────────────────────────────────────

class TestPreprocessImageForOcr:
    """Tests for _preprocess_image_for_ocr."""

    def test_returns_binary_image(self):
        img = Image.new("RGB", (100, 100), color=(200, 200, 200))
        result = sc._preprocess_image_for_ocr(img)
        assert result.mode == "1"  # binary

    def test_handles_dark_image(self):
        img = Image.new("RGB", (50, 50), color=(10, 10, 10))
        result = sc._preprocess_image_for_ocr(img)
        assert result.size == (50, 50)
        assert result.mode == "1"


# ── _page_needs_ocr ──────────────────────────────────────────────────────────

class TestPageNeedsOcr:
    """Tests for _page_needs_ocr heuristic."""

    def test_empty_text_needs_ocr(self):
        # We need a mock page; the function only uses the text parameter
        assert sc._page_needs_ocr(None, "") is True
        assert sc._page_needs_ocr(None, "   ") is True

    def test_very_short_text_needs_ocr(self):
        assert sc._page_needs_ocr(None, "abc") is True

    def test_garbled_text_needs_ocr(self):
        # Mostly non-alpha characters
        assert sc._page_needs_ocr(None, "!@#$%^&*()_+-=[]{}") is True

    def test_normal_paragraph_does_not_need_ocr(self):
        text = "This is a normal paragraph with sufficient meaningful text content for testing."
        assert sc._page_needs_ocr(None, text) is False

    def test_twenty_nine_alpha_needs_ocr(self):
        # 29 meaningful chars — below threshold of 30
        text = "a" * 29
        assert sc._page_needs_ocr(None, text) is True

    def test_thirty_alpha_does_not_need_ocr(self):
        # 30 meaningful chars — at threshold
        text = "a" * 30
        assert sc._page_needs_ocr(None, text) is False


# ── _quick_text_layer_scan ────────────────────────────────────────────────────

class TestQuickTextLayerScan:
    """Tests for _quick_text_layer_scan."""

    def _write_pdf(self, path, page_texts):
        """Create a PDF with one page per entry; empty string = blank page."""
        doc = sc.fitz.open()
        for text in page_texts:
            page = doc.new_page(width=595, height=842)
            if text:
                page.insert_text((50, 100), text, fontsize=14)
        doc.save(str(path))
        doc.close()

    def test_text_pdf_reports_native_text(self, tmp_path):
        pdf_path = tmp_path / "text.pdf"
        self._write_pdf(pdf_path, ["This page has a proper native text layer for scanning tests."])
        total, sampled, with_text = sc._quick_text_layer_scan(str(pdf_path))
        assert total == 1
        assert sampled == 1
        assert with_text == 1

    def test_blank_pdf_reports_no_native_text(self, tmp_path):
        pdf_path = tmp_path / "blank.pdf"
        self._write_pdf(pdf_path, [""])
        total, sampled, with_text = sc._quick_text_layer_scan(str(pdf_path))
        assert total == 1
        assert sampled == 1
        assert with_text == 0

    def test_multi_page_sampling_caps_at_max_samples(self, tmp_path):
        pdf_path = tmp_path / "multi.pdf"
        self._write_pdf(
            pdf_path,
            [f"Page {i+1} with native text content for the sampling scan." for i in range(20)],
        )
        total, sampled, with_text = sc._quick_text_layer_scan(str(pdf_path))
        assert total == 20
        assert sampled == 5  # default max_samples
        assert with_text == 5

    def test_partial_text_layer_detected(self, tmp_path):
        pdf_path = tmp_path / "partial.pdf"
        self._write_pdf(
            pdf_path,
            [f"Text page {i+1} with native text content for the sampling scan."
             if i % 2 == 0 else "" for i in range(6)],
        )
        total, sampled, with_text = sc._quick_text_layer_scan(str(pdf_path))
        assert total == 6
        assert sampled == 5  # 6 pages > max_samples=5 → evenly spaced subset
        assert 0 < with_text < sampled  # mix of text and blank pages


# ── Integration: extract_text_from_pdf (with a generated PDF) ────────────────

class TestExtractTextFromPdf:
    """Integration tests for extract_text_from_pdf with a real PDF."""

    @pytest.fixture
    def sample_pdf(self, tmp_path):
        """Create a minimal single-page PDF with text."""
        # Use PyMuPDF to create a simple PDF
        doc = sc.fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 100), "Hello EPUB World!", fontsize=20)
        page.insert_text((50, 200), "This is a test paragraph.", fontsize=14)
        pdf_path = tmp_path / "test.pdf"
        doc.save(str(pdf_path))
        doc.close()
        return pdf_path

    def test_include_images_flag(self, sample_pdf):
        """extract_text_from_pdf handles include_images=False/True without crashing."""
        # Without images flag
        pages_no_img = sc.extract_text_from_pdf(str(sample_pdf), include_images=False)
        for p in pages_no_img:
            assert p["images"] == []

        # With images flag — generated PDF has no embedded images, so the list
        # may still be empty, but the flag itself must work (no crash)
        pages_with_img = sc.extract_text_from_pdf(str(sample_pdf), include_images=True)
        for p in pages_with_img:
            assert isinstance(p["images"], list)


# ── _resolve_book_metadata ───────────────────────────────────────────────────

class TestResolveBookMetadata:
    """Tests for _resolve_book_metadata."""

    def test_pdf_metadata_takes_precedence(self):
        assert sc._resolve_book_metadata(
            {"title": "PDF Title", "author": "PDF Author"},
            llm_title="LLM Title", llm_author="LLM Author",
        ) == ("PDF Title", "PDF Author")

    def test_llm_values_fill_empty_metadata(self):
        assert sc._resolve_book_metadata(
            {"title": "", "author": ""},
            llm_title="The Historian's Craft", llm_author="Marc Bloch",
        ) == ("The Historian's Craft", "Marc Bloch")

    def test_placeholder_llm_values_ignored(self):
        assert sc._resolve_book_metadata(
            {"title": "", "author": ""},
            llm_title="Untitled", llm_author="Unknown",
        ) == ("Untitled", "Unknown")

    def test_no_llm_values(self):
        assert sc._resolve_book_metadata(
            {"title": "", "author": ""},
        ) == ("Untitled", "Unknown")

    def test_partial_fallback(self):
        assert sc._resolve_book_metadata(
            {"title": "", "author": "PDF Author"},
            llm_title="LLM Title", llm_author="LLM Author",
        ) == ("LLM Title", "PDF Author")


# ── build_epub_from_llm_output: OPF metadata override ────────────────────────

class TestLlmMetadataOverride:
    """LLM-reported title/author fill OPF metadata for scanned books."""

    CHAPTERS = [{"title": "Intro", "content": "<p>Hello.</p>"}]

    def _build(self, tmp_path, chapters_data, metadata):
        epub_path = tmp_path / "out.epub"
        sc.build_epub_from_llm_output(chapters_data, metadata, str(epub_path))
        return epub_path

    def test_llm_title_and_author_fill_empty_opf_metadata(self, tmp_path):
        epub_path = self._build(
            tmp_path,
            {"title": "The Historian's Craft", "author": "Marc Bloch",
             "chapters": self.CHAPTERS},
            {"title": "", "author": ""},
        )
        with zipfile.ZipFile(epub_path) as zf:
            opf = zf.read("OEBPS/content.opf").decode("utf-8")
        # Apostrophe is XML-escaped by _escape_xml
        assert "<dc:title>The Historian&apos;s Craft</dc:title>" in opf
        assert "<dc:creator>Marc Bloch</dc:creator>" in opf

    def test_placeholder_chapters_data_does_not_override_real_metadata(self, tmp_path):
        epub_path = self._build(
            tmp_path,
            {"title": "Untitled", "author": "Unknown", "chapters": self.CHAPTERS},
            {"title": "Real Book", "author": "Real Author"},
        )
        with zipfile.ZipFile(epub_path) as zf:
            opf = zf.read("OEBPS/content.opf").decode("utf-8")
        assert "<dc:title>Real Book</dc:title>" in opf
        assert "<dc:creator>Real Author</dc:creator>" in opf

    def test_real_metadata_not_overwritten_by_llm_values(self, tmp_path):
        epub_path = self._build(
            tmp_path,
            {"title": "LLM Title", "author": "LLM Author",
             "chapters": self.CHAPTERS},
            {"title": "Real Book", "author": "Real Author"},
        )
        with zipfile.ZipFile(epub_path) as zf:
            opf = zf.read("OEBPS/content.opf").decode("utf-8")
        assert "<dc:title>Real Book</dc:title>" in opf
        assert "<dc:creator>Real Author</dc:creator>" in opf


# ── _extract_blocks_from_page ────────────────────────────────────────────────

class TestExtractBlocksFromPage:
    """Tests for _extract_blocks_from_page."""

    def test_extracts_blocks_from_text_page(self):
        doc = sc.fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 100), "Heading", fontsize=20)
        page.insert_text((50, 200), "Paragraph text here.", fontsize=12)

        blocks = sc._extract_blocks_from_page(page)
        doc.close()

        # Should have at least 2 blocks (heading + paragraph)
        assert len(blocks) >= 2
        texts = [b["text"] for b in blocks]
        assert any("Heading" in t for t in texts)
        assert any("Paragraph" in t for t in texts)


