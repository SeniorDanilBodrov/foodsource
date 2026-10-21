"""Optional local semantic ranking powered by rubert-tiny2 in ONNX Runtime.

The module is deliberately optional: production and the static demo continue to
work when the model or its Python dependencies are absent.  Supplier embeddings
are cached in memory and rebuilt only when the searchable catalogue changes.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_DIR = PROJECT_ROOT / "models" / "rubert-tiny2"


def supplier_document(supplier: dict) -> str:
    """Build a compact, structured document without noisy scraped navigation."""
    categories = ", ".join(supplier.get("categories", []))
    products = ", ".join(supplier.get("products", []))
    regions = ", ".join(supplier.get("regions", []))
    parts = [
        f"Поставщик: {supplier.get('name', '')}.",
        f"Тип: {supplier.get('type', '')}.",
        f"Категории: {categories}.",
        f"Товары и ассортимент: {products}.",
        f"Регионы работы: {regions}.",
    ]
    # Curated/operator descriptions can add useful vocabulary. Automatically
    # scraped descriptions are intentionally excluded because they often contain
    # menus, cookie banners and SEO text.
    if supplier.get("collection_mode") in {"reviewed_metadata", "operator_import"}:
        description = " ".join(str(supplier.get("description", "")).split())[:300]
        if description:
            parts.append(f"Описание: {description}.")
    return " ".join(parts)


def supplier_chunks(supplier: dict) -> list[str]:
    """Represent a card with short chunks so one relevant product is not diluted."""
    chunks = [supplier_document(supplier)]
    chunks.extend(str(value) for value in supplier.get("categories", []) if value)
    chunks.extend(str(value) for value in supplier.get("products", []) if value)
    # Deduplicate without changing the order; stable order keeps the cache stable.
    return list(dict.fromkeys(" ".join(chunk.split()) for chunk in chunks if chunk.strip()))


class SemanticEngine:
    """Lazy, thread-safe ONNX sentence encoder with an in-memory document cache."""

    def __init__(self) -> None:
        self.model_dir = Path(os.getenv("FOODSOURCE_MODEL_DIR", str(DEFAULT_MODEL_DIR)))
        self.mode = os.getenv("FOODSOURCE_SEMANTIC", "auto").strip().lower()
        self._session = None
        self._tokenizer = None
        self._error = ""
        self._model_name = "rubert-tiny2-int8"
        self._chunk_vectors: dict[str, object] = {}
        self._query_vectors: dict[str, object] = {}
        self._lock = threading.Lock()

    @property
    def allowed(self) -> bool:
        return self.mode not in {"0", "false", "off", "disabled"}

    def _model_path(self) -> Path:
        quantized = self.model_dir / "model.int8.onnx"
        return quantized if quantized.exists() else self.model_dir / "model.onnx"

    def ensure_loaded(self) -> bool:
        if self._session is not None and self._tokenizer is not None:
            return True
        if not self.allowed:
            self._error = "отключён настройкой FOODSOURCE_SEMANTIC"
            return False
        with self._lock:
            if self._session is not None and self._tokenizer is not None:
                return True
            model_path = self._model_path()
            tokenizer_path = self.model_dir / "tokenizer.json"
            if not model_path.exists() or not tokenizer_path.exists():
                self._error = f"файлы модели не найдены в {self.model_dir}"
                return False
            try:
                import onnxruntime as ort
                from tokenizers import Tokenizer

                options = ort.SessionOptions()
                options.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
                options.inter_op_num_threads = 1
                self._session = ort.InferenceSession(
                    str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
                )
                self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
                self._tokenizer.enable_truncation(max_length=128)
                self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
                self._model_name = "rubert-tiny2-int8" if ".int8." in model_path.name else "rubert-tiny2"
                self._error = ""
                return True
            except Exception as exc:  # the deterministic search remains available
                self._session = None
                self._tokenizer = None
                self._error = str(exc)
                return False

    def status(self, load: bool = False) -> dict:
        if load:
            self.ensure_loaded()
        return {
            "enabled": self.allowed,
            "available": self._session is not None and self._tokenizer is not None,
            "model": self._model_name if self._session is not None else None,
            "mode": "hybrid" if self._session is not None else "deterministic",
            "error": self._error or None,
            "cached_documents": len(self._chunk_vectors),
        }

    def _encode(self, texts: Iterable[str]):
        import numpy as np

        if not self.ensure_loaded():
            raise RuntimeError(self._error or "семантическая модель недоступна")
        values = list(texts)
        encoded = self._tokenizer.encode_batch(values)
        input_ids = np.asarray([item.ids for item in encoded], dtype=np.int64)
        attention_mask = np.asarray([item.attention_mask for item in encoded], dtype=np.int64)
        vectors = self._session.run(
            ["sentence_embedding"],
            {"input_ids": input_ids, "attention_mask": attention_mask},
        )[0]
        # The exported model already normalizes embeddings, but repeating the
        # normalization protects us if the model artifact is replaced later.
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.clip(norms, 1e-12, None)

    def _document_vectors(self, suppliers: list[dict]):
        """Reuse chunk embeddings across searches with different candidate pools."""
        import numpy as np

        texts: list[str] = []
        owners: list[str] = []
        for supplier in suppliers:
            supplier_id = str(supplier.get("id", ""))
            chunks = supplier_chunks(supplier)
            texts.extend(chunks)
            owners.extend([supplier_id] * len(chunks))

        with self._lock:
            missing = list(dict.fromkeys(text for text in texts if text not in self._chunk_vectors))
        # Small batches keep peak memory predictable on the free Render instance.
        for start in range(0, len(missing), 16):
            batch = missing[start:start + 16]
            vectors = self._encode(batch)
            with self._lock:
                self._chunk_vectors.update(zip(batch, vectors))
        with self._lock:
            return np.stack([self._chunk_vectors[text] for text in texts]), owners

    def prewarm(self, suppliers: list[dict]) -> dict:
        """Build the supplier cache before the service accepts search traffic."""
        if not suppliers or not self.ensure_loaded():
            return self.status()
        try:
            self._document_vectors(suppliers)
        except Exception as exc:
            self._error = str(exc)
        return self.status()

    def score(self, query: str, suppliers: list[dict]) -> dict[str, float]:
        """Return cosine similarity by supplier id, or an empty map on fallback."""
        query = " ".join(query.split()).strip()
        if not query or not suppliers or not self.ensure_loaded():
            return {}
        # Tiny BERT can assign deceptively high similarities to an invented word
        # assembled entirely from subword fragments.  Require at least one
        # recognisable content token before semantic expansion; exact/rule search
        # still handles the query when this guard rejects it.
        encoded_query = self._tokenizer.encode(query.lower())
        content_tokens = [
            token for token in encoded_query.tokens
            if token not in {"[CLS]", "[SEP]", "[PAD]"} and token not in {"для", "или", "при", "это", "как"}
        ]
        if not any(not token.startswith("##") and len(token) >= 4 for token in content_tokens):
            return {}
        try:
            document_vectors, owners = self._document_vectors(suppliers)
            with self._lock:
                query_vector = self._query_vectors.get(query)
            if query_vector is None:
                query_vector = self._encode([query])[0]
                with self._lock:
                    if len(self._query_vectors) >= 256:
                        self._query_vectors.pop(next(iter(self._query_vectors)))
                    self._query_vectors[query] = query_vector
            similarities = document_vectors @ query_vector
            result: dict[str, float] = {}
            for supplier_id, similarity in zip(owners, similarities):
                result[supplier_id] = max(result.get(supplier_id, -1.0), float(similarity))
            return result
        except Exception as exc:
            self._error = str(exc)
            return {}


semantic_engine = SemanticEngine()
