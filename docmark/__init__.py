"""DocMark — drop-in desktop document → Markdown converter."""

from docmark.pipeline import DocumentPipeline, ProgressEvent
from docmark.config import Settings

__all__ = ["DocumentPipeline", "ProgressEvent", "Settings"]
