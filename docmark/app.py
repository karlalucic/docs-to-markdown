"""PyWebView shell for DocMark: drop a file, get clean Markdown.

Drag-drop is bound on the Python side via webview.dom.DOMEventHandler — the
event payload exposes the real local file paths in `pywebviewFullPath`, which
browser JS drag events deliberately do not.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import webview
from webview.dom import DOMEventHandler

from docmark.config import Settings, cache_dir
from docmark.pipeline import DocumentPipeline, ProgressEvent
from docmark.utils import secrets

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

UI_DIR = Path(__file__).resolve().parent / "ui"
SUPPORTED_EXTS = {".pdf", ".docx", ".pptx", ".ppt", ".xlsx"}
FILE_DIALOG_TYPES = ("Documents (*.pdf;*.docx;*.pptx;*.ppt;*.xlsx)",)


@dataclass
class Job:
    job_id: str
    path: Path
    status: str = "queued"
    percent: float = 0.0
    message: str = ""
    output: Optional[str] = None
    error: Optional[str] = None


class JsApi:
    """Methods exposed to the UI as `window.pywebview.api.*`."""

    def __init__(self) -> None:
        self._window: Optional[webview.Window] = None
        self._settings = Settings.load()
        self._pipeline = self._build_pipeline()
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="docmark-job")
        self._jobs: Dict[str, Job] = {}
        self._pending_batches: Dict[str, List[Path]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def set_window(self, window: webview.Window) -> None:
        self._window = window

    def _build_pipeline(self) -> DocumentPipeline:
        return DocumentPipeline(self._settings)

    # ------------------------------------------------------------------
    # Job dispatch (called from Python on_drop and from JS retry)
    # ------------------------------------------------------------------

    def enqueue(self, paths: List[str]) -> List[Dict]:
        """Queue files or folders for conversion. Returns the visible job rows."""
        return self._enqueue_documents(self._collect_document_paths(paths))

    def prepare_conversion(self, paths: List[str]) -> Dict:
        """Prepare a selected batch and return a confirmation preview."""
        documents, skipped_count = self._split_unconverted_documents(
            self._collect_document_paths(paths)
        )
        if not documents:
            return self._selection_to_dict(None, [], skipped_count=skipped_count)

        selection_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._pending_batches[selection_id] = documents
        return self._selection_to_dict(
            selection_id, documents, skipped_count=skipped_count
        )

    def confirm_selection(self, selection_id: str) -> List[Dict]:
        """Start converting a previously prepared batch."""
        with self._lock:
            documents = self._pending_batches.pop(selection_id, [])
        return self._enqueue_documents(documents)

    def cancel_selection(self, selection_id: str) -> None:
        """Forget a prepared batch without converting it."""
        with self._lock:
            self._pending_batches.pop(selection_id, None)

    def _enqueue_documents(self, documents: Iterable[Path]) -> List[Dict]:
        results: List[Dict] = []
        accepted: List[Job] = []
        for path in documents:
            existing_output = DocumentPipeline.current_output_path(path)
            if existing_output is not None:
                job = Job(
                    job_id=uuid.uuid4().hex[:12],
                    path=path,
                    status="done",
                    percent=100.0,
                    message=f"Already converted: {existing_output.name}",
                    output=str(existing_output),
                )
            else:
                job = Job(job_id=uuid.uuid4().hex[:12], path=path)
                accepted.append(job)
            with self._lock:
                self._jobs[job.job_id] = job
            results.append(self._job_to_dict(job))

        for job in results:
            self._push_to_ui(job)

        for job in accepted:
            self._pool.submit(self._run_job, job.job_id)
        return results

    @staticmethod
    def _selection_to_dict(
        selection_id: Optional[str],
        documents: List[Path],
        skipped_count: int = 0,
    ) -> Dict:
        preview_limit = 20
        files = [
            {"filename": path.name, "path": str(path)}
            for path in documents[:preview_limit]
        ]
        return {
            "selection_id": selection_id,
            "total": len(documents),
            "files": files,
            "hidden_count": max(0, len(documents) - preview_limit),
            "skipped_count": skipped_count,
        }

    @staticmethod
    def _split_unconverted_documents(documents: List[Path]) -> tuple[List[Path], int]:
        remaining: List[Path] = []
        skipped_count = 0
        for path in documents:
            if DocumentPipeline.current_output_path(path) is not None:
                skipped_count += 1
            else:
                remaining.append(path)
        return remaining, skipped_count

    @staticmethod
    def _collect_document_paths(paths: Iterable[str]) -> List[Path]:
        """Expand files and directories into a de-duplicated, stable file list."""
        documents: List[Path] = []
        seen: set[str] = set()

        for raw in paths:
            try:
                path = Path(raw).expanduser().resolve()
            except Exception:  # noqa: BLE001
                continue

            if path.is_dir():
                try:
                    candidates = sorted(
                        (p for p in path.rglob("*") if p.is_file()),
                        key=lambda p: str(p).lower(),
                    )
                except OSError:
                    logger.warning("Could not scan dropped folder: %s", path)
                    continue
            else:
                candidates = [path]

            for candidate in candidates:
                if not candidate.is_file():
                    continue
                if candidate.suffix.lower() not in SUPPORTED_EXTS:
                    continue
                key = str(candidate)
                if key in seen:
                    continue
                seen.add(key)
                documents.append(candidate)

        return documents

    def pick_files(self) -> Dict:
        """Open the native file picker and prepare selected files for confirmation."""
        if self._window is None:
            return self._selection_to_dict(None, [])
        selection = self._window.create_file_dialog(
            webview.FileDialog.OPEN,
            allow_multiple=True,
            file_types=FILE_DIALOG_TYPES,
        )
        if not selection:
            return self._selection_to_dict(None, [])
        return self.prepare_conversion(list(selection))

    def pick_folder(self) -> Dict:
        """Open the native folder picker and prepare supported files for confirmation."""
        if self._window is None:
            return self._selection_to_dict(None, [])
        selection = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        if not selection:
            return self._selection_to_dict(None, [])
        if isinstance(selection, str):
            return self.prepare_conversion([selection])
        return self.prepare_conversion(list(selection))

    def retry_job(self, job_id: str) -> Optional[Dict]:
        """Retry a failed job in-place so PDF checkpoints can resume."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.status != "failed":
                return self._job_to_dict(job)
            job.status = "queued"
            job.percent = 0.0
            job.message = "Retry queued"
            job.error = None
            job.output = None
            payload = self._job_to_dict(job)

        self._push_to_ui(payload)
        self._pool.submit(self._run_job, job_id)
        return payload

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return

        try:
            last_message = ""

            def cb(event: ProgressEvent) -> None:
                nonlocal last_message
                last_message = event.message
                self._update(
                    job_id,
                    status="converting",
                    percent=event.percent,
                    message=event.message,
                )

            out_path = self._pipeline.convert_file(job.path, progress_callback=cb)
            self._update(
                job_id,
                status="done",
                percent=100.0,
                message=last_message or f"Wrote {out_path.name}",
                output=str(out_path),
                error=None,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Job %s failed", job_id)
            self._update(job_id, status="failed", message=str(e), error=str(e))

    def _update(self, job_id: str, **fields) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for k, v in fields.items():
                setattr(job, k, v)
            payload = self._job_to_dict(job)
        self._push_to_ui(payload)

    def _push_to_ui(self, payload: Dict) -> None:
        if self._window is None:
            return
        try:
            self._window.evaluate_js(
                f"window.onJobUpdate && window.onJobUpdate({json.dumps(payload)})"
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to push job update to UI")

    def _push_selection_to_ui(self, payload: Dict) -> None:
        if self._window is None:
            return
        try:
            self._window.evaluate_js(
                f"window.onSelectionPreview && window.onSelectionPreview({json.dumps(payload)})"
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to push selection preview to UI")

    @staticmethod
    def _job_to_dict(job: Job) -> Dict:
        return {
            "job_id": job.job_id,
            "filename": job.path.name,
            "path": str(job.path),
            "status": job.status,
            "percent": round(job.percent, 1),
            "message": job.message,
            "output": job.output,
            "error": job.error,
        }

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def get_settings(self) -> Dict:
        return self._settings.to_public_dict()

    def save_settings(self, payload: Dict) -> Dict:
        # API key handling: only update if a non-empty string is provided.
        # `null` means "clear", absent means "leave unchanged".
        key_field = payload.get("openai_api_key", "__absent__")
        if key_field == "__absent__":
            pass
        elif key_field in (None, ""):
            secrets.delete_openai_key()
            self._settings.openai_api_key = None
        elif isinstance(key_field, str):
            secrets.set_openai_key(key_field.strip())
            self._settings.openai_api_key = key_field.strip()

        editable = {
            "model",
            "dpi",
            "detail",
            "force_vision",
            "office_vision_pptx_enabled",
            "docx_image_description_enabled",
            "cache_enabled",
        }
        for field in editable:
            if field in payload:
                setattr(self._settings, field, payload[field])

        self._settings.save()
        # Rebuild pipeline so new settings (key, model, dpi, …) take effect.
        self._pipeline = self._build_pipeline()
        return self._settings.to_public_dict()

    def clear_cache(self) -> Dict:
        """Delete cached conversion results stored by DocMark."""
        deleted = 0
        bytes_deleted = 0
        path = cache_dir()

        for cache_file in path.glob("*.json"):
            try:
                bytes_deleted += cache_file.stat().st_size
                cache_file.unlink()
                deleted += 1
            except FileNotFoundError:
                continue
            except OSError:
                logger.exception("Failed to delete cache file: %s", cache_file)

        return {"deleted": deleted, "bytes_deleted": bytes_deleted}

    # ------------------------------------------------------------------
    # OS integration
    # ------------------------------------------------------------------

    def reveal_in_finder(self, path: str) -> None:
        target = Path(path).expanduser()
        if not target.exists():
            return
        try:
            if sys.platform == "darwin":
                subprocess.run(["open", "-R", str(target)], check=False)
            elif sys.platform.startswith("win"):
                subprocess.run(["explorer", "/select,", str(target)], check=False)
            else:
                subprocess.run(["xdg-open", str(target.parent)], check=False)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to reveal %s", target)


def _wire_dom(api: JsApi, window: webview.Window) -> None:
    """Bind the Python-side `drop` handler so we can read full local paths."""

    def on_drop(event):
        # `pywebviewFullPath` is the canonical pywebview-injected payload.
        paths = event.get("pywebviewFullPath") or []
        if isinstance(paths, str):
            paths = [paths]
        # Fallback: try `dataTransfer.files` names (no full path on browsers).
        if not paths:
            try:
                files = event.get("dataTransfer", {}).get("files", [])
                paths = [f.get("name") for f in files if f.get("name")]
            except Exception:  # noqa: BLE001
                paths = []
        if paths:
            api._push_selection_to_ui(api.prepare_conversion([str(p) for p in paths]))

    window.dom.document.events.drop += DOMEventHandler(
        on_drop, prevent_default=True, stop_propagation=True
    )

    # Prevent the webview from navigating to the dropped file when dragover fires.
    window.dom.document.events.dragover += DOMEventHandler(
        lambda e: None, prevent_default=True, stop_propagation=True
    )


def main() -> None:
    api = JsApi()
    index = UI_DIR / "index.html"
    if not index.exists():
        raise FileNotFoundError(
            f"UI bundle not found at {index}. "
            "Reinstall DocMark from a package that includes docmark/ui assets."
        )

    window = webview.create_window(
        title="DocMark",
        url=str(index),
        js_api=api,
        width=720,
        height=560,
        min_size=(520, 420),
    )
    api.set_window(window)

    webview.start(lambda w: _wire_dom(api, w), window)


if __name__ == "__main__":
    main()
