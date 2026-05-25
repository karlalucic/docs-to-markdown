"""Document → Markdown pipeline orchestrator.

PDF: Docling first, OpenAI vision fallback on low-confidence pages.
DOCX / XLSX / PPTX: native parsers (python-docx, openpyxl, python-pptx).
PPTX gets optional per-slide vision rendering when heuristics fire.

TODO(v2): unify DOCX/PPTX/XLSX on Docling's DocumentConverter +
    export_to_markdown(). PPTX/XLSX paths in Docling are still less mature
    than the native parsers as of 2026.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, List, Literal, Optional

from docmark.config import Settings, cache_dir
from docmark.models.document_schema import (
    BlockType,
    ContentBlock,
    DocumentResult,
    ExtractionMethod,
    ImageContent,
    PageResult,
    ProcessingSummary,
    TableContent,
)
from docmark.parsers.docling_parser import DoclingParser
from docmark.parsers.vision_parser import VisionModelParser
from docmark.utils.cache_manager import CacheManager
from docmark.utils.office_utils import (
    DOCXParser,
    PPTXParser,
    XLSXParser,
    get_document_type,
)
from docmark.utils.pdf_utils import get_pdf_page_count

logger = logging.getLogger(__name__)

CACHE_SCHEMA_VERSION = "docmark-v1"
CHECKPOINT_VERSION = 1


def _file_fingerprint(path: Path) -> str:
    """Quick identity string for a file: first-1 MB MD5 + mtime (int seconds)."""
    with open(path, "rb") as fh:
        sample = fh.read(1024 * 1024)
    digest = hashlib.md5(sample).hexdigest()[:16]  # noqa: S324 — identity, not security
    mtime = int(path.stat().st_mtime)
    return f"{digest}_{mtime}"

ProgressStage = Literal[
    "loading", "docling", "vision", "office", "writing", "done"
]


@dataclass
class ProgressEvent:
    """One progress update emitted during a single-file conversion."""

    stage: ProgressStage
    percent: float
    message: str


ProgressCallback = Callable[[ProgressEvent], None]


def _emit(callback: Optional[ProgressCallback], event: ProgressEvent) -> None:
    if callback is None:
        return
    try:
        callback(event)
    except Exception:  # noqa: BLE001 — callbacks must never break conversion
        logger.exception("Progress callback raised")


class HeuristicEvaluator:
    """Routes individual PDF pages from Docling to the vision model."""

    def __init__(self, settings: Settings):
        self.s = settings
        self._patterns = [re.compile(p) for p in settings.technical_param_patterns]

    def evaluate_page(self, page: PageResult) -> tuple[bool, List[str]]:
        reasons: List[str] = []

        if len(page.blocks) == 0:
            return True, ["No content extracted — page may be scanned/image-only"]

        total_text = sum(
            len(str(b.content))
            for b in page.blocks
            if b.type in (BlockType.TEXT, BlockType.HEADING)
        )
        if total_text < self.s.min_content_threshold:
            reasons.append(
                f"Minimal text ({total_text} chars) — possible scanned image"
            )

        for idx, block in enumerate(page.blocks, start=1):
            if block.type == BlockType.TABLE:
                conf = getattr(block.content, "extraction_confidence", 0.90)
                if conf < self.s.table_confidence_threshold:
                    reasons.append(f"Table {idx} low confidence: {conf:.2f}")
            elif block.type == BlockType.IMAGE:
                conf = getattr(block.content, "extraction_confidence", 0.90)
                if conf < self.s.picture_confidence_threshold:
                    reasons.append(f"Image {idx} low confidence: {conf:.2f}")
                desc = getattr(block.content, "description", "") or ""
                if len(desc) < self.s.picture_description_min_length:
                    reasons.append(
                        f"Image {idx} missing description ({len(desc)} chars)"
                    )

        if self._patterns:
            all_text = " ".join(
                str(b.content)
                for b in page.blocks
                if b.type in (BlockType.TEXT, BlockType.HEADING)
                and isinstance(b.content, str)
            )
            count = sum(len(p.findall(all_text)) for p in self._patterns)
            table_count = sum(1 for b in page.blocks if b.type == BlockType.TABLE)
            if count >= self.s.technical_param_threshold and table_count == 0:
                reasons.append(
                    f"High technical density ({count} params) with no tables"
                )

        return bool(reasons), reasons

    def check_merged_cells(
        self, docling_result: Any, page_idx: int
    ) -> tuple[bool, List[str]]:
        doc = getattr(docling_result, "document", None)
        if doc is None or not hasattr(doc, "tables"):
            return False, []

        for table_idx, table in enumerate(doc.tables, start=1):
            if hasattr(table, "prov") and table.prov:
                table_page = getattr(table.prov[0], "page_no", None)
                if table_page is not None and table_page != page_idx + 1:
                    continue

            data = getattr(table, "data", None)
            grid = getattr(data, "grid", None) if data is not None else None
            if grid is None:
                continue
            for row in grid:
                for cell in row:
                    if (
                        getattr(cell, "row_span", 1) > 1
                        or getattr(cell, "col_span", 1) > 1
                    ):
                        return True, [f"Merged cell in Table {table_idx}"]

        return False, []


class DocumentPipeline:
    """Convert documents (PDF/DOCX/XLSX/PPTX/PPT) to Markdown."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.tier1 = DoclingParser(
            table_confidence_threshold=settings.table_confidence_threshold
        )
        self.tier3: Optional[VisionModelParser] = None
        if settings.openai_api_key:
            self.tier3 = VisionModelParser(
                api_key=settings.openai_api_key,
                model=settings.model,
                dpi=settings.dpi,
                detail=settings.detail,
            )
        self.heuristics = HeuristicEvaluator(settings)
        self.cache = CacheManager(cache_dir()) if settings.cache_enabled else None
        self._config_hash = self._build_config_hash(settings)
        self._libreoffice_cmd = self._check_libreoffice_available()

    @staticmethod
    def _build_config_hash(settings: Settings) -> str:
        payload = asdict(settings)
        payload.pop("openai_api_key", None)
        payload["has_openai_api_key"] = bool(settings.openai_api_key)
        payload["schema"] = CACHE_SCHEMA_VERSION
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _check_libreoffice_available() -> Optional[str]:
        return (
            shutil.which("soffice")
            or shutil.which("libreoffice")
            or (
                "/Applications/LibreOffice.app/Contents/MacOS/soffice"
                if os.path.isfile(
                    "/Applications/LibreOffice.app/Contents/MacOS/soffice"
                )
                else None
            )
        )

    # ------------------------------------------------------------------
    # Checkpoint helpers (crash recovery / resume)
    # ------------------------------------------------------------------

    @staticmethod
    def _checkpoint_path(source: Path) -> Path:
        """Hidden sidecar file next to the source document."""
        return source.parent / f".{source.name}.docmark-progress"

    def _load_checkpoint(self, source: Path) -> dict[int, PageResult]:
        """Load per-page results from a previous interrupted run.

        Returns an empty dict if no valid checkpoint exists.
        """
        cp = self._checkpoint_path(source)
        if not cp.exists():
            return {}
        try:
            data = json.loads(cp.read_text(encoding="utf-8"))
            if data.get("version") != CHECKPOINT_VERSION:
                return {}
            if data.get("config_hash") != self._config_hash:
                return {}
            if data.get("file_fingerprint") != _file_fingerprint(source):
                return {}
            pages: dict[int, PageResult] = {}
            for k, v in data.get("pages", {}).items():
                pages[int(k)] = PageResult(**v)
            logger.info("Checkpoint: loaded %d pages for %s", len(pages), source.name)
            return pages
        except Exception:  # noqa: BLE001
            logger.debug("Could not load checkpoint for %s — starting fresh", source.name)
            return {}

    def _write_checkpoint(
        self, source: Path, pages: dict[int, PageResult]
    ) -> None:
        """Atomically persist completed page results for crash recovery."""
        cp = self._checkpoint_path(source)
        try:
            data: dict = {
                "version": CHECKPOINT_VERSION,
                "config_hash": self._config_hash,
                "file_fingerprint": _file_fingerprint(source),
                "pages": {str(k): v.model_dump() for k, v in pages.items()},
            }
            tmp = cp.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.rename(cp)
        except Exception:  # noqa: BLE001
            pass  # Checkpoint is best-effort; never break conversion

    def _remove_checkpoint(self, source: Path) -> None:
        try:
            self._checkpoint_path(source).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def convert_file(
        self,
        path: Path,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> Path:
        """Convert a single file to Markdown and write it next to the source.

        Returns the path of the written .md file. Raises on unrecoverable failure.
        On PDF files the pipeline writes a per-page checkpoint after every page
        so that a restart can resume without re-spending API credits.
        """
        path = Path(path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Not a file: {path}")

        checkpoint = self._load_checkpoint(path)
        if checkpoint:
            msg = f"Resuming {path.name} — {len(checkpoint)} pages already done"
        else:
            msg = f"Loading {path.name}"

        _emit(
            progress_callback,
            ProgressEvent(stage="loading", percent=0.0, message=msg),
        )

        result = self._process_document(path, progress_callback, checkpoint=checkpoint)

        _emit(
            progress_callback,
            ProgressEvent(stage="writing", percent=95.0, message="Writing Markdown"),
        )
        out_path = self._unique_output_path(path)
        out_path.write_text(self._render_markdown(result), encoding="utf-8")

        # Conversion succeeded — remove the in-progress checkpoint.
        self._remove_checkpoint(path)

        _emit(
            progress_callback,
            ProgressEvent(stage="done", percent=100.0, message=f"Wrote {out_path.name}"),
        )
        return out_path

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def _process_document(
        self,
        path: Path,
        cb: Optional[ProgressCallback],
        checkpoint: Optional[dict[int, "PageResult"]] = None,
    ) -> DocumentResult:
        if self.cache is not None:
            cached = self.cache.load_document_cache(path, config_hash=self._config_hash)
            if cached is not None:
                _emit(
                    cb,
                    ProgressEvent(stage="docling", percent=90.0, message="Cache hit"),
                )
                return cached

        doc_type = get_document_type(path)
        if doc_type == "pdf":
            result = self._process_pdf(path, cb, checkpoint=checkpoint or {})
        elif doc_type == "docx":
            result = self._process_docx(path, cb)
        elif doc_type == "xlsx":
            result = self._process_xlsx(path, cb)
        elif doc_type == "xls":
            raise ValueError(
                f"Legacy .xls is not supported: {path.name}. Convert to .xlsx and retry."
            )
        elif doc_type == "pptx":
            result = self._process_pptx(path, cb)
        elif doc_type == "ppt":
            result = self._process_ppt_legacy(path, cb)
        else:
            raise ValueError(f"Unsupported document type: {doc_type}")

        if self.cache is not None:
            self.cache.save_document_cache(path, result, config_hash=self._config_hash)
        return result

    # ------------------------------------------------------------------
    # PDF
    # ------------------------------------------------------------------

    def _process_pdf(
        self,
        pdf_path: Path,
        cb: Optional[ProgressCallback],
        checkpoint: Optional[dict[int, "PageResult"]] = None,
    ) -> DocumentResult:
        """Process a PDF file with per-page checkpoint support.

        ``checkpoint`` maps already-completed page numbers → their PageResult.
        Pages present in the checkpoint are reused as-is; we only run Docling
        (and potentially vision) for the remaining pages.  After every new page
        is finalised the checkpoint file is updated so a crash only loses
        work for the page in flight.
        """
        doc_start = time.time()
        try:
            page_count = get_pdf_page_count(pdf_path)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"Cannot open '{pdf_path.name}' — the file may be corrupt, "
                f"password-protected, or in an unsupported PDF variant. ({e})"
            ) from e
        checkpoint = dict(checkpoint or {})  # mutable working copy

        pages_needed = [p for p in range(1, page_count + 1) if p not in checkpoint]

        docling_result: Any = None
        tier1_pages: List[PageResult] = []
        tier1_error: Optional[str] = None

        if pages_needed and not self.settings.force_vision:
            try:
                docling_result, tier1_pages = self.tier1.parse_document(pdf_path)
            except Exception as e:  # noqa: BLE001
                tier1_error = str(e)
                logger.error("Docling failed for %s: %s", pdf_path.name, e)
        elif not pages_needed:
            logger.info("All %d pages loaded from checkpoint for %s", page_count, pdf_path.name)
        elif self.settings.force_vision:
            tier1_error = "force_vision enabled"

        pages: List[PageResult] = []
        summary = ProcessingSummary()

        for page_num in range(1, page_count + 1):
            percent = 10.0 + (page_num - 1) / max(page_count, 1) * 80.0

            # Reuse checkpoint result — no Docling or vision needed for this page.
            if page_num in checkpoint:
                pages.append(checkpoint[page_num])
                _emit(
                    cb,
                    ProgressEvent(
                        stage="docling",
                        percent=percent,
                        message=f"Page {page_num}/{page_count} (resumed)",
                    ),
                )
                summary.tier1_pages += 1
                continue

            _emit(
                cb,
                ProgressEvent(
                    stage="docling",
                    percent=percent,
                    message=f"Page {page_num}/{page_count}",
                ),
            )

            tier1_result: Optional[PageResult] = None
            if tier1_error is None and page_num - 1 < len(tier1_pages):
                tier1_result = tier1_pages[page_num - 1]

            page_result = self._route_pdf_page(
                pdf_path=pdf_path,
                page_num=page_num,
                summary=summary,
                tier1_result=tier1_result,
                docling_result=docling_result,
                tier1_error=tier1_error,
                cb=cb,
                page_percent_base=percent,
                page_percent_span=80.0 / max(page_count, 1),
            )
            pages.append(page_result)

            # Persist this page so a crash can resume from here.
            checkpoint[page_num] = page_result
            self._write_checkpoint(pdf_path, checkpoint)

        summary.total_processing_time_seconds = time.time() - doc_start
        overall_confidence = (
            sum(p.confidence for p in pages) / len(pages) if pages else 0.0
        )

        return DocumentResult(
            doc_id=pdf_path.stem,
            filename=pdf_path.name,
            total_pages=page_count,
            processing_summary=summary,
            pages=pages,
            overall_confidence=overall_confidence,
        )

    def _route_pdf_page(
        self,
        pdf_path: Path,
        page_num: int,
        summary: ProcessingSummary,
        tier1_result: Optional[PageResult],
        docling_result: Any,
        tier1_error: Optional[str],
        cb: Optional[ProgressCallback],
        page_percent_base: float,
        page_percent_span: float,
    ) -> PageResult:
        tier1_failed = tier1_result is None
        needs_vision = False
        reasons: List[str] = []

        if self.settings.force_vision:
            needs_vision = True
            reasons.append("force_vision enabled")
        elif not tier1_failed:
            if tier1_result.confidence < self.settings.pdf_confidence_threshold:
                needs_vision = True
                reasons.append(
                    f"Low Docling confidence: {tier1_result.confidence:.2f} < "
                    f"{self.settings.pdf_confidence_threshold:.2f}"
                )

            heuristic_needs_vision, heuristic_reasons = self.heuristics.evaluate_page(
                tier1_result
            )
            if heuristic_needs_vision:
                needs_vision = True
                reasons.extend(heuristic_reasons)

            if not needs_vision and docling_result is not None:
                merged_needs_vision, merged_reasons = self.heuristics.check_merged_cells(
                    docling_result, page_num - 1
                )
                if merged_needs_vision:
                    needs_vision = True
                    reasons.extend(merged_reasons)

        if needs_vision or tier1_failed:
            if self.tier3 is None:
                if tier1_result is not None:
                    tier1_result.warnings.append(
                        "Vision fallback skipped: no OpenAI API key configured"
                    )
                    tier1_result.warnings.extend(reasons)
                    summary.tier1_pages += 1
                    return tier1_result
                raise RuntimeError(
                    "Vision fallback required for this page but no OpenAI API key is "
                    "configured. Add a key in Settings."
                )

            _emit(
                cb,
                ProgressEvent(
                    stage="vision",
                    percent=page_percent_base + page_percent_span * 0.5,
                    message=f"Vision: page {page_num}",
                ),
            )
            try:
                vision_result = self._run_vision_cached(pdf_path, page_num)
                summary.tier3_pages += 1
                summary.total_api_calls += 1
                if tier1_failed:
                    vision_result.warnings.append(
                        f"Tier 1 failed: {tier1_error or 'unknown error'}"
                    )
                else:
                    vision_result.warnings.extend(reasons)
                return vision_result
            except Exception as vision_error:  # noqa: BLE001
                logger.error("Vision failed for page %s: %s", page_num, vision_error)
                if tier1_result is not None:
                    tier1_result.warnings.append(
                        f"Vision enhancement failed: {vision_error}"
                    )
                    tier1_result.warnings.extend(reasons)
                    summary.tier1_pages += 1
                    return tier1_result
                return PageResult(
                    page_num=page_num,
                    extraction_method=ExtractionMethod.DOCLING,
                    confidence=0.0,
                    processing_time_seconds=0.0,
                    blocks=[],
                    warnings=[
                        f"Tier 1 error: {tier1_error or 'unknown error'}",
                        f"Vision error: {vision_error}",
                    ],
                    fallback_triggered=True,
                )

        summary.tier1_pages += 1
        return tier1_result

    def _run_vision_cached(self, pdf_path: Path, page_num: int) -> PageResult:
        if self.cache is not None:
            cached = self.cache.load_page_cache(
                pdf_path, page_num, "tier3", config_hash=self._config_hash
            )
            if cached is not None:
                return cached
        assert self.tier3 is not None  # guarded by caller
        result = self.tier3.enhance_page(pdf_path, page_num)
        if self.cache is not None:
            self.cache.save_page_cache(
                pdf_path, page_num, "tier3", result, config_hash=self._config_hash
            )
        return result

    # ------------------------------------------------------------------
    # DOCX
    # ------------------------------------------------------------------

    def _process_docx(
        self, docx_path: Path, cb: Optional[ProgressCallback]
    ) -> DocumentResult:
        _emit(
            cb,
            ProgressEvent(stage="office", percent=40.0, message=f"Parsing {docx_path.name}"),
        )
        start = time.time()
        do_image_descriptions = (
            self.settings.docx_image_description_enabled and self.tier3 is not None
        )

        if do_image_descriptions:
            blocks, image_payloads = DOCXParser.parse_document_with_images(docx_path)
        else:
            blocks = DOCXParser.parse_document(docx_path)
            image_payloads = []

        summary = ProcessingSummary(tier1_pages=1)
        page_result = PageResult(
            page_num=1,
            extraction_method=ExtractionMethod.DOCLING,
            confidence=_compute_office_confidence(blocks),
            processing_time_seconds=time.time() - start,
            blocks=blocks,
            warnings=[],
        )

        if do_image_descriptions and self.tier3 is not None:
            for payload in image_payloads:
                if not 0 <= payload.block_index < len(page_result.blocks):
                    continue
                block = page_result.blocks[payload.block_index]
                if block.type != BlockType.IMAGE or not isinstance(
                    block.content, ImageContent
                ):
                    continue
                try:
                    desc = self.tier3.describe_image_bytes(
                        payload.image_bytes, payload.mime_type
                    )
                    block.content.description = desc
                    block.content.extraction_confidence = 0.95
                    summary.total_api_calls += 1
                except Exception as e:  # noqa: BLE001
                    page_result.warnings.append(
                        f"Image {payload.sequence} description failed: {e}"
                    )

        page_result.confidence = _compute_office_confidence(page_result.blocks)
        summary.total_processing_time_seconds = time.time() - start

        return DocumentResult(
            doc_id=docx_path.stem,
            filename=docx_path.name,
            total_pages=1,
            processing_summary=summary,
            pages=[page_result],
            overall_confidence=page_result.confidence,
        )

    # ------------------------------------------------------------------
    # XLSX
    # ------------------------------------------------------------------

    def _process_xlsx(
        self, xlsx_path: Path, cb: Optional[ProgressCallback]
    ) -> DocumentResult:
        _emit(
            cb,
            ProgressEvent(stage="office", percent=40.0, message=f"Parsing {xlsx_path.name}"),
        )
        start = time.time()
        blocks = XLSXParser.parse_document(xlsx_path)
        confidence = _compute_office_confidence(blocks)
        elapsed = time.time() - start

        page_result = PageResult(
            page_num=1,
            extraction_method=ExtractionMethod.DOCLING,
            confidence=confidence,
            processing_time_seconds=elapsed,
            blocks=blocks,
            warnings=[],
        )
        summary = ProcessingSummary(
            tier1_pages=1, total_processing_time_seconds=elapsed
        )

        return DocumentResult(
            doc_id=xlsx_path.stem,
            filename=xlsx_path.name,
            total_pages=1,
            processing_summary=summary,
            pages=[page_result],
            overall_confidence=confidence,
        )

    # ------------------------------------------------------------------
    # PPTX
    # ------------------------------------------------------------------

    def _process_pptx(
        self, pptx_path: Path, cb: Optional[ProgressCallback]
    ) -> DocumentResult:
        _emit(
            cb,
            ProgressEvent(stage="office", percent=20.0, message=f"Parsing {pptx_path.name}"),
        )
        start = time.time()
        slides_data = PPTXParser.parse_document(pptx_path)

        pages: List[PageResult] = []
        summary = ProcessingSummary()
        for slide_num, blocks in slides_data:
            pages.append(
                PageResult(
                    page_num=slide_num,
                    extraction_method=ExtractionMethod.DOCLING,
                    confidence=_compute_office_confidence(blocks),
                    processing_time_seconds=0.0,
                    blocks=blocks,
                    warnings=[],
                )
            )
            summary.tier1_pages += 1

        do_vision = self.settings.office_vision_pptx_enabled and self.tier3 is not None
        if do_vision:
            triggered = [
                (p.page_num, self.heuristics.evaluate_page(p)[1])
                for p in pages
                if self.heuristics.evaluate_page(p)[0]
            ]
            if triggered:
                pdf_path = self._convert_office_to_pdf(pptx_path)
                if pdf_path is not None and get_pdf_page_count(pdf_path) == len(pages):
                    try:
                        for slide_num, reasons in triggered:
                            idx = slide_num - 1
                            _emit(
                                cb,
                                ProgressEvent(
                                    stage="vision",
                                    percent=50.0 + idx / len(pages) * 40.0,
                                    message=f"Vision: slide {slide_num}",
                                ),
                            )
                            try:
                                assert self.tier3 is not None
                                vision_result = self.tier3.enhance_page(
                                    pdf_path, slide_num
                                )
                                vision_result.warnings.extend(reasons)
                                pages[idx] = vision_result
                                summary.tier1_pages -= 1
                                summary.tier3_pages += 1
                                summary.total_api_calls += 1
                            except Exception as e:  # noqa: BLE001
                                pages[idx].warnings.append(
                                    f"Vision failed for slide {slide_num}: {e}"
                                )
                                pages[idx].warnings.extend(reasons)
                    finally:
                        shutil.rmtree(pdf_path.parent, ignore_errors=True)
                else:
                    for slide_num, reasons in triggered:
                        pages[slide_num - 1].warnings.append(
                            "Vision skipped: PDF conversion failed or page count mismatch"
                        )
                        pages[slide_num - 1].warnings.extend(reasons)

        summary.total_processing_time_seconds = time.time() - start
        overall_confidence = (
            sum(p.confidence for p in pages) / len(pages) if pages else 0.0
        )

        return DocumentResult(
            doc_id=pptx_path.stem,
            filename=pptx_path.name,
            total_pages=len(pages),
            processing_summary=summary,
            pages=pages,
            overall_confidence=overall_confidence,
        )

    # ------------------------------------------------------------------
    # Legacy .ppt
    # ------------------------------------------------------------------

    def _process_ppt_legacy(
        self, ppt_path: Path, cb: Optional[ProgressCallback]
    ) -> DocumentResult:
        pdf_path = self._convert_office_to_pdf(ppt_path)
        if pdf_path is None:
            raise ValueError(
                f"Cannot process .ppt file without LibreOffice: {ppt_path.name}. "
                "Install LibreOffice or save the file as .pptx."
            )
        try:
            result = self._process_pdf(pdf_path, cb)
            result.doc_id = ppt_path.stem
            result.filename = ppt_path.name
            return result
        finally:
            shutil.rmtree(pdf_path.parent, ignore_errors=True)

    def _convert_office_to_pdf(self, office_path: Path) -> Optional[Path]:
        if self._libreoffice_cmd is None:
            logger.warning("LibreOffice not found — Office vision/PPT conversion disabled")
            return None
        temp_dir = Path(tempfile.mkdtemp(prefix="docmark-"))
        try:
            subprocess.run(
                [
                    self._libreoffice_cmd,
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(temp_dir),
                    str(office_path),
                ],
                check=True,
                timeout=120,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            expected = temp_dir / f"{office_path.stem}.pdf"
            if expected.exists():
                return expected
        except Exception as e:  # noqa: BLE001
            logger.warning("LibreOffice conversion failed: %s", e)
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None

    # ------------------------------------------------------------------
    # Output rendering
    # ------------------------------------------------------------------

    @staticmethod
    def _unique_output_path(source: Path) -> Path:
        candidate = source.with_suffix(".md")
        if not candidate.exists():
            return candidate
        for i in range(1, 1000):
            alt = source.with_name(f"{source.stem}-{i}.md")
            if not alt.exists():
                return alt
        raise FileExistsError(
            f"Could not find a free filename next to {source} after 1000 tries"
        )

    @staticmethod
    def _render_markdown(result: DocumentResult) -> str:
        """Render a DocumentResult as clean Markdown — no metadata headers."""
        multi_page = result.total_pages > 1
        out: List[str] = []

        for page in result.pages:
            page_lines = _render_blocks(page.blocks)
            if not page_lines:
                continue
            if multi_page and out:
                out.append("")
                out.append("---")
                out.append("")
            out.extend(page_lines)

        # Drop trailing blank lines
        while out and not out[-1].strip():
            out.pop()
        return "\n".join(out) + "\n"


def _render_blocks(blocks: List[ContentBlock]) -> List[str]:
    lines: List[str] = []
    for block in blocks:
        btype = block.type if isinstance(block.type, BlockType) else BlockType(block.type)
        if btype == BlockType.HEADING:
            level = block.level or 1
            lines.append(f"{'#' * level} {block.content}")
            lines.append("")
        elif btype == BlockType.PAGE_HEADER or btype == BlockType.PAGE_FOOTER:
            # Skip running headers/footers in clean output
            continue
        elif btype == BlockType.TABLE:
            if isinstance(block.content, TableContent):
                lines.append(block.content.markdown)
            else:
                lines.append(str(block.content))
            lines.append("")
        elif btype == BlockType.CODE:
            lines.append("```")
            lines.append(str(block.content))
            lines.append("```")
            lines.append("")
        elif btype == BlockType.IMAGE:
            if isinstance(block.content, ImageContent):
                if block.content.description:
                    if block.content.caption:
                        lines.append(f"**{block.content.caption}**")
                    lines.append(block.content.description)
                    lines.append("")
                elif block.content.caption:
                    lines.append(f"*[{block.content.caption}]*")
                    lines.append("")
            else:
                lines.append(str(block.content))
                lines.append("")
        elif btype == BlockType.LIST:
            lines.append(str(block.content))
            lines.append("")
        else:  # TEXT
            lines.append(str(block.content))
            lines.append("")
    return lines


def _compute_office_confidence(blocks: List[ContentBlock]) -> float:
    if not blocks:
        return 0.0
    scored = [
        b.content.extraction_confidence
        for b in blocks
        if b.type in {BlockType.TABLE, BlockType.IMAGE}
        and hasattr(b.content, "extraction_confidence")
        and b.content.extraction_confidence is not None
        and not math.isnan(b.content.extraction_confidence)
    ]
    if scored:
        return min(scored)
    text_types = {
        BlockType.TEXT,
        BlockType.HEADING,
        BlockType.LIST,
        BlockType.CODE,
        BlockType.PAGE_HEADER,
        BlockType.PAGE_FOOTER,
    }
    if any(b.type in text_types for b in blocks):
        return 0.90
    return 0.5
