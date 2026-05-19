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
from typing import Dict, List, Optional

import webview
from webview.dom import DOMEventHandler

from docmark.config import Settings
from docmark.pipeline import DocumentPipeline, ProgressEvent
from docmark.utils import secrets

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
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
        """Queue one or more files for conversion. Returns the visible job rows."""
        if not paths:
            return []

        results: List[Dict] = []
        accepted: List[Job] = []
        for raw in paths:
            try:
                path = Path(raw).expanduser().resolve()
            except Exception:  # noqa: BLE001
                continue
            if not path.is_file():
                continue
            if path.suffix.lower() not in SUPPORTED_EXTS:
                continue
            job = Job(job_id=uuid.uuid4().hex[:12], path=path)
            with self._lock:
                self._jobs[job.job_id] = job
            accepted.append(job)
            results.append(self._job_to_dict(job))

        for job in accepted:
            self._pool.submit(self._run_job, job.job_id)
        return results

    def pick_files(self) -> List[Dict]:
        """Open the native file picker and enqueue whatever the user selects."""
        if self._window is None:
            return []
        selection = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=True,
            file_types=FILE_DIALOG_TYPES,
        )
        if not selection:
            return []
        return self.enqueue(list(selection))

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return

        try:
            def cb(event: ProgressEvent) -> None:
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
                message=f"Wrote {out_path.name}",
                output=str(out_path),
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
            api.enqueue([str(p) for p in paths])

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
            "Run from a checkout that includes the ui/ directory."
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
