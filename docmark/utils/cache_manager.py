"""
Cache manager for intermediate processing results.
"""

from pathlib import Path
from typing import Optional
import json
import hashlib
import uuid
from docmark.models.document_schema import PageResult, DocumentResult


class CacheManager:
    """
    Manage intermediate processing results to avoid reprocessing.

    Caches:
    - Tier 1 results per document
    - Tier 3 results per page
    """

    def __init__(self, cache_dir: Path):
        """
        Initialize cache manager.

        Args:
            cache_dir: Directory to store cache files
        """
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        _chmod_best_effort(self.cache_dir, 0o700)

    def get_cache_key(
        self, pdf_path: Path, page_num: Optional[int] = None,
        config_hash: Optional[str] = None
    ) -> str:
        """
        Generate cache key from file path, optional page number, and config hash.

        Args:
            pdf_path: Path to document file
            page_num: Optional page number
            config_hash: Optional config hash to version cache by settings

        Returns:
            Cache key string
        """
        # Use file hash + modification time for cache key
        with open(pdf_path, "rb") as f:
            # Read first 1MB for hash (faster than full file)
            file_sample = f.read(1024 * 1024)
            file_hash = hashlib.md5(file_sample).hexdigest()[:16]

        mtime = int(pdf_path.stat().st_mtime)

        key = f"{pdf_path.stem}_{file_hash}_{mtime}"
        if config_hash:
            key += f"_{config_hash}"
        if page_num is not None:
            key += f"_{page_num}"
        return key

    def load_document_cache(
        self, pdf_path: Path, config_hash: Optional[str] = None
    ) -> Optional[DocumentResult]:
        """
        Load cached document result if available.

        Args:
            pdf_path: Path to document file
            config_hash: Optional config hash for cache versioning

        Returns:
            DocumentResult if cached, None otherwise
        """
        cache_key = self.get_cache_key(pdf_path, config_hash=config_hash)
        cache_file = self.cache_dir / f"{cache_key}_doc.json"

        if cache_file.exists():
            try:
                data = json.loads(cache_file.read_text())
                return DocumentResult(**data)
            except Exception:
                # Cache corrupted, ignore
                return None

        return None

    def save_document_cache(
        self, pdf_path: Path, result: DocumentResult,
        config_hash: Optional[str] = None
    ):
        """
        Save document result to cache.

        Args:
            pdf_path: Path to document file
            result: DocumentResult to cache
            config_hash: Optional config hash for cache versioning
        """
        cache_key = self.get_cache_key(pdf_path, config_hash=config_hash)
        cache_file = self.cache_dir / f"{cache_key}_doc.json"

        _write_private_json(cache_file, result.to_dict())

    def load_page_cache(
        self, pdf_path: Path, page_num: int, tier: str,
        config_hash: Optional[str] = None
    ) -> Optional[PageResult]:
        """
        Load cached page result for specific tier.

        Args:
            pdf_path: Path to document file
            page_num: Page number (1-indexed)
            tier: Tier name ("tier1", "tier3")
            config_hash: Optional config hash for cache versioning

        Returns:
            PageResult if cached, None otherwise
        """
        cache_key = self.get_cache_key(pdf_path, page_num, config_hash=config_hash)
        cache_file = self.cache_dir / f"{cache_key}_{tier}.json"

        if cache_file.exists():
            try:
                data = json.loads(cache_file.read_text())
                return PageResult(**data)
            except Exception:
                # Cache corrupted, ignore
                return None

        return None

    def save_page_cache(
        self, pdf_path: Path, page_num: int, tier: str, result: PageResult,
        config_hash: Optional[str] = None
    ):
        """
        Save page result to cache.

        Args:
            pdf_path: Path to document file
            page_num: Page number (1-indexed)
            tier: Tier name ("tier1", "tier3")
            result: PageResult to cache
            config_hash: Optional config hash for cache versioning
        """
        cache_key = self.get_cache_key(pdf_path, page_num, config_hash=config_hash)
        cache_file = self.cache_dir / f"{cache_key}_{tier}.json"

        _write_private_json(cache_file, result.model_dump())

    def clear_cache(self, pdf_path: Optional[Path] = None):
        """
        Clear cache for specific document or entire cache.

        Args:
            pdf_path: Optional path to PDF file. If None, clears entire cache
        """
        if pdf_path:
            cache_key = self.get_cache_key(pdf_path)
            pattern = f"{cache_key}*"
            for cache_file in self.cache_dir.glob(pattern):
                cache_file.unlink()
        else:
            for cache_file in self.cache_dir.glob("*.json"):
                cache_file.unlink()


def _write_private_json(path: Path, payload: dict) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _chmod_best_effort(tmp, 0o600)
        tmp.replace(path)
        _chmod_best_effort(path, 0o600)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _chmod_best_effort(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass
