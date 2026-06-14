"""
RAG Indexer — ChromaDB + sentence-transformers
================================================
Tách biệt hoàn toàn khỏi agent logic.

Luồng:
  Agent 1 ghi file  →  rag.index_file(path)   →  ChromaDB
  Agent 2 hỏi       →  rag.query(text, k=5)   →  List[str] chunks
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy imports — chỉ load khi dùng lần đầu
# ---------------------------------------------------------------------------
_chroma_client = None
_collection = None
_embedder = None

_CHROMA_DIR = Path(__file__).parent / "chroma_db"
_COLLECTION_NAME = "merchant_analytics"
_CHUNK_SIZE = 500       # ký tự mỗi chunk
_CHUNK_OVERLAP = 80     # overlap giữa các chunk


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_embedder():
    global _embedder
    if _embedder is None:
        try:
            from sentence_transformers import SentenceTransformer
            _embedder = SentenceTransformer("all-MiniLM-L6-v2")
            logger.info("RAG: SentenceTransformer loaded (all-MiniLM-L6-v2)")
        except Exception as exc:
            logger.warning(f"RAG: sentence-transformers not available — {exc}")
            _embedder = None
    return _embedder


def _get_collection():
    global _chroma_client, _collection
    if _collection is None:
        try:
            import chromadb
            _CHROMA_DIR.mkdir(parents=True, exist_ok=True)
            _chroma_client = chromadb.PersistentClient(path=str(_CHROMA_DIR))
            _collection = _chroma_client.get_or_create_collection(
                name=_COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
            logger.info(f"RAG: ChromaDB collection '{_COLLECTION_NAME}' ready ({_collection.count()} docs)")
        except Exception as exc:
            logger.warning(f"RAG: ChromaDB not available — {exc}")
            _collection = None
    return _collection


def _chunk_text(text: str) -> list[str]:
    """Chia text thành chunks có overlap."""
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + _CHUNK_SIZE, len(text))
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start += _CHUNK_SIZE - _CHUNK_OVERLAP
    return chunks


def _file_hash(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _doc_id(path: Path, chunk_idx: int) -> str:
    return f"{path.stem}__{chunk_idx}"


def _is_already_indexed(collection, path: Path) -> bool:
    """Kiểm tra file đã được index với cùng nội dung chưa (dùng hash)."""
    try:
        result = collection.get(
            where={"source": path.name},
            limit=1,
            include=["metadatas"],
        )
        if result["metadatas"]:
            stored_hash = result["metadatas"][0].get("file_hash", "")
            return stored_hash == _file_hash(path)
    except Exception:
        pass
    return False


def _read_file_text(path: Path) -> Optional[str]:
    """Đọc nội dung file hỗ trợ: .md, .txt, .json, .csv."""
    try:
        suffix = path.suffix.lower()
        if suffix in (".md", ".txt"):
            return path.read_text(encoding="utf-8", errors="ignore")
        if suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            return json.dumps(data, ensure_ascii=False, indent=2)
        if suffix == ".csv":
            # Chỉ lấy 200 dòng đầu để tránh quá nặng
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            return "\n".join(lines[:200])
    except Exception as exc:
        logger.warning(f"RAG: cannot read {path.name} — {exc}")
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def index_file(path: str | Path) -> bool:
    """
    Index một file vào ChromaDB.
    - Bỏ qua nếu file không thay đổi (same hash).
    - Xóa chunks cũ của file trước khi re-index.
    Returns True nếu index thành công.
    """
    path = Path(path)
    if not path.exists():
        return False

    embedder = _get_embedder()
    collection = _get_collection()
    if embedder is None or collection is None:
        return False

    # Skip nếu không thay đổi
    if _is_already_indexed(collection, path):
        logger.debug(f"RAG: {path.name} unchanged, skip")
        return True

    text = _read_file_text(path)
    if not text:
        return False

    chunks = _chunk_text(text)
    if not chunks:
        return False

    # Xóa chunks cũ của file này
    try:
        collection.delete(where={"source": path.name})
    except Exception:
        pass

    file_hash = _file_hash(path)
    ids = [_doc_id(path, i) for i in range(len(chunks))]
    metadatas = [
        {
            "source": path.name,
            "file_hash": file_hash,
            "chunk_idx": i,
            "file_type": path.suffix.lstrip("."),
        }
        for i in range(len(chunks))
    ]

    try:
        embeddings = embedder.encode(chunks, show_progress_bar=False).tolist()
        collection.add(
            ids=ids,
            documents=chunks,
            embeddings=embeddings,
            metadatas=metadatas,
        )
        logger.info(f"RAG: indexed {path.name} → {len(chunks)} chunks")
        return True
    except Exception as exc:
        logger.warning(f"RAG: failed to index {path.name} — {exc}")
        return False


def index_directory(directory: str | Path, pattern: str = "*.md") -> int:
    """
    Index toàn bộ file trong thư mục khớp pattern.
    Dùng khi startup để sync các file cũ chưa được index.
    Returns số file đã index.
    """
    directory = Path(directory)
    if not directory.exists():
        return 0
    count = 0
    for path in sorted(directory.glob(pattern), key=lambda f: f.stat().st_mtime):
        if index_file(path):
            count += 1
    return count


def query(text: str, k: int = 5, file_type: Optional[str] = None) -> list[str]:
    """
    Tìm k chunks liên quan nhất với text.
    Args:
        text: Câu hỏi hoặc keyword cần tìm.
        k: Số chunks trả về.
        file_type: Lọc theo loại file ("md", "json", "csv") — None = tất cả.
    Returns:
        Danh sách chunk text, sắp xếp theo độ liên quan.
    """
    embedder = _get_embedder()
    collection = _get_collection()
    if embedder is None or collection is None:
        return []

    total = collection.count()
    if total == 0:
        return []

    k = min(k, total)
    try:
        query_embedding = embedder.encode([text], show_progress_bar=False).tolist()
        where_filter = {"file_type": file_type} if file_type else None
        results = collection.query(
            query_embeddings=query_embedding,
            n_results=k,
            where=where_filter,
            include=["documents", "metadatas", "distances"],
        )
        chunks = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]

        # Format mỗi chunk kèm source file
        formatted = []
        for chunk, meta in zip(chunks, metadatas):
            source = meta.get("source", "unknown")
            formatted.append(f"[{source}]\n{chunk}")
        return formatted
    except Exception as exc:
        logger.warning(f"RAG: query failed — {exc}")
        return []


def query_as_context(text: str, k: int = 5) -> str:
    """
    Wrapper tiện lợi: trả về string context để nhúng vào prompt.
    """
    chunks = query(text, k=k)
    if not chunks:
        return ""
    separator = "\n\n---\n\n"
    return f"### Thông tin lịch sử liên quan:\n\n" + separator.join(chunks)


def stats() -> dict:
    """Trả về thống kê ChromaDB collection."""
    collection = _get_collection()
    if collection is None:
        return {"available": False}
    return {
        "available": True,
        "total_chunks": collection.count(),
        "chroma_dir": str(_CHROMA_DIR),
    }
