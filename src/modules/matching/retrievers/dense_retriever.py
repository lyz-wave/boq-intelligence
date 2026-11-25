import hashlib
import os
from pathlib import Path
import threading
from typing import Any, Optional

import numpy as np
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import normalize

from src.core.contracts.boq_item import StandardizedBoqItem
from src.core.contracts.matching import MatchContext
from src.core.registry import PluginRegistry

_PREFERRED_HF_MODELS = ("BAAI/bge-m3", "BAAI/bge-base-zh-v1.5")
_SHARED_ST_LOCK = threading.Lock()
_SHARED_ST_CACHE: dict[tuple[str, str], Any] = {}


def _best_torch_device() -> str:
    try:
        import torch

        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def download_hf_model(repo_id: str = "BAAI/bge-m3") -> dict[str, Any]:
    """按需从 HuggingFace / HF-Mirror 下载编码模型到本地缓存（离线不可达时安全返回错误说明）."""
    snap = _find_local_hf_snapshot(repo_id)
    if snap is not None:
        return {"ok": True, "repo_id": repo_id, "already_cached": True, "snapshot": str(snap)}
    try:
        from huggingface_hub import snapshot_download

        path = snapshot_download(repo_id=repo_id)
        return {"ok": True, "repo_id": repo_id, "already_cached": False, "snapshot": str(path)}
    except Exception as exc:
        return {"ok": False, "repo_id": repo_id, "already_cached": False, "error": str(exc)}


def _hf_cache_dir() -> Path:
    env_hub = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    if env_hub:
        return Path(env_hub)
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return Path(hf_home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _find_local_hf_snapshot(repo_id: str) -> Optional[Path]:
    folder = _hf_cache_dir() / ("models--" + repo_id.replace("/", "--")) / "snapshots"
    if not folder.exists():
        return None
    for snap in sorted(folder.iterdir()):
        if not snap.is_dir():
            continue
        has_config = (snap / "config.json").exists()
        has_weights = (snap / "model.safetensors").exists() or (snap / "pytorch_model.bin").exists()
        has_tok = (snap / "tokenizer.json").exists() or (snap / "tokenizer_config.json").exists() or (snap / "sentencepiece.bpe.model").exists()
        if has_config and has_weights and has_tok:
            return snap
    return None


@PluginRegistry.register("retrievers")
class DenseSemanticRetriever:
    """稠密语义向量召回插件：优先加载本地 BGE-M3 / BGE-Base-ZH 神经网络向量并持久化至 SQLite，缺失时平滑退避 SVD."""

    retriever_id: str = "dense"

    def __init__(self, n_components: int = 128, backend: str = "auto") -> None:
        self.n_components = n_components
        self._requested_backend = backend
        self._active_model_name = "svd-char-ngram-128"
        self._backend_kind = "svd"
        self._device = "cpu"
        self._dim = n_components

        self._st_model: Any = None
        self._st_lock = threading.Lock()
        self._query_cache: dict[tuple[str, str], np.ndarray] = {}

        self._vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), lowercase=True)
        self._svd: TruncatedSVD | None = None
        self._doc_matrix: np.ndarray | None = None
        self._item_ids: list[str] = []
        self._id_to_idx: dict[str, int] = {}
        self._items_map: dict[str, StandardizedBoqItem] = {}

    def _resolve_target_model(self) -> tuple[str, str, Optional[Path]]:
        req = os.environ.get("BOQ_DENSE_BACKEND", self._requested_backend).strip().lower()
        in_pytest = bool(os.environ.get("PYTEST_CURRENT_TEST")) and os.environ.get("BOQ_ENABLE_NEURAL_IN_TESTS") != "1"
        if req == "svd" or (in_pytest and req == "auto"):
            return "svd", "svd-char-ngram-128", None

        try:
            import sentence_transformers  # noqa: F401
            import torch  # noqa: F401
        except Exception:
            return "svd", "svd-char-ngram-128", None

        candidates = (
            [req]
            if req not in ("auto", "neural", "bge", "svd")
            else list(_PREFERRED_HF_MODELS)
        )
        for repo_id in candidates:
            # 规范名称大小写
            norm_id = next((m for m in _PREFERRED_HF_MODELS if m.lower() == repo_id.lower()), repo_id)
            snap = _find_local_hf_snapshot(norm_id)
            if snap is not None:
                return "bge", norm_id, snap

        return "svd", "svd-char-ngram-128", None

    def _ensure_st_model(self) -> Any:
        with self._st_lock:
            if self._st_model is not None and self._active_model_name == getattr(self, "_loaded_st_name", None):
                return self._st_model
            from sentence_transformers import SentenceTransformer

            dev = _best_torch_device()
            snap = _find_local_hf_snapshot(self._active_model_name)
            load_target = str(snap) if snap is not None else self._active_model_name
            cache_key = (load_target, dev)
            with _SHARED_ST_LOCK:
                model = _SHARED_ST_CACHE.get(cache_key)
                if model is None:
                    model = SentenceTransformer(load_target, device=dev, local_files_only=(snap is not None))
                    _SHARED_ST_CACHE[cache_key] = model
            self._device = dev
            self._st_model = model
            self._loaded_st_name = self._active_model_name
            return model

    def _encode_neural_batch(self, texts: list[str]) -> np.ndarray:
        model = self._ensure_st_model()
        vecs = model.encode(
            texts,
            normalize_embeddings=True,
            batch_size=32,
            show_progress_bar=False,
        )
        return np.asarray(vecs, dtype=np.float32)

    def build_index(self, items: list[StandardizedBoqItem], repo: Optional[Any] = None) -> None:
        self._item_ids = [it.item_id for it in items]
        self._id_to_idx = {iid: idx for idx, iid in enumerate(self._item_ids)}
        self._items_map = {it.item_id: it for it in items}
        self._query_cache.clear()

        if not items:
            self._doc_matrix = None
            return

        kind, model_name, _snap = self._resolve_target_model()
        self._backend_kind = kind
        self._active_model_name = model_name
        if kind == "bge":
            self._device = _best_torch_device()

        if kind == "bge":
            try:
                corpus_pairs = [
                    (
                        it.item_id,
                        f"{it.standard_name} {it.canonical_search_text}",
                        hashlib.md5(f"{it.standard_name} {it.canonical_search_text}".encode("utf-8")).hexdigest()[:16],
                    )
                    for it in items
                ]
                cached_map: dict[str, tuple[str, bytes, int]] = (
                    repo.load_embeddings(model_name) if repo is not None and hasattr(repo, "load_embeddings") else {}
                )

                vectors: list[Optional[np.ndarray]] = [None] * len(items)
                missing_indices: list[int] = []
                detected_dim: Optional[int] = None

                for idx, (iid, _txt, t_hash) in enumerate(corpus_pairs):
                    rec = cached_map.get(iid)
                    if rec is not None and rec[0] == t_hash:
                        arr = np.frombuffer(rec[1], dtype=np.float32).copy()
                        if len(arr) == rec[2] and rec[2] > 0:
                            vectors[idx] = arr
                            detected_dim = rec[2]
                            continue
                    missing_indices.append(idx)

                if missing_indices:
                    texts_to_encode = [corpus_pairs[i][1] for i in missing_indices]
                    encoded = self._encode_neural_batch(texts_to_encode)
                    detected_dim = int(encoded.shape[1])
                    to_save: list[tuple[str, str, int, bytes]] = []
                    for pos, orig_idx in enumerate(missing_indices):
                        vec = np.asarray(encoded[pos], dtype=np.float32)
                        vectors[orig_idx] = vec
                        iid, _txt, t_hash = corpus_pairs[orig_idx]
                        to_save.append((iid, t_hash, detected_dim, vec.tobytes()))
                    if repo is not None and hasattr(repo, "save_embeddings"):
                        repo.save_embeddings(model_name, to_save)

                mat = np.vstack([v for v in vectors if v is not None]).astype(np.float32)
                self._doc_matrix = mat
                self._dim = int(mat.shape[1]) if mat.ndim == 2 else (detected_dim or 768)
                return
            except Exception:
                # 若本地模型权重损坏或推理异常，安全退避至 SVD
                self._backend_kind = "svd"
                self._active_model_name = "svd-char-ngram-128"

        # SVD 确定性轻量语义子空间
        corpus = [f"{it.standard_name} {it.canonical_search_text}" for it in items]
        tfidf = self._vectorizer.fit_transform(corpus)
        n_comp = min(self.n_components, max(2, tfidf.shape[0] - 1), max(2, tfidf.shape[1] - 1))
        if tfidf.shape[0] >= 3 and tfidf.shape[1] >= 3:
            self._svd = TruncatedSVD(n_components=n_comp, random_state=42)
            dense = self._svd.fit_transform(tfidf)
            self._doc_matrix = normalize(dense, norm="l2").astype(np.float32)
        else:
            self._svd = None
            self._doc_matrix = normalize(tfidf.toarray(), norm="l2").astype(np.float32)
        self._dim = int(self._doc_matrix.shape[1]) if self._doc_matrix is not None else 0

    def encode_text(self, text: str) -> np.ndarray:
        c_key = (self._active_model_name, text)
        cached = self._query_cache.get(c_key)
        if cached is not None:
            return cached

        if self._backend_kind == "bge":
            vec = self._encode_neural_batch([text])[0]
        else:
            t_vec = self._vectorizer.transform([text])
            if self._svd is not None:
                dense = self._svd.transform(t_vec)
            else:
                dense = t_vec.toarray()
            vec = normalize(dense, norm="l2")[0].astype(np.float32)

        if len(self._query_cache) > 4000:
            self._query_cache.clear()
        self._query_cache[c_key] = vec
        return vec

    def doc_embedding_for(self, item_id: str) -> Optional[np.ndarray]:
        idx = self._id_to_idx.get(item_id)
        if idx is None or self._doc_matrix is None:
            return None
        return self._doc_matrix[idx]

    def export_doc_matrix(self) -> tuple[list[str], Optional[np.ndarray]]:
        return list(self._item_ids), self._doc_matrix

    def similarity(self, text_a: str, text_b: str) -> float:
        if not self._item_ids:
            return 0.0
        va = self.encode_text(text_a)
        vb = self.encode_text(text_b)
        return float(max(0.0, float(np.dot(va, vb))))

    def retrieve(self, query: StandardizedBoqItem, ctx: MatchContext, top_n: int) -> list[tuple[str, float]]:
        if self._doc_matrix is None or not self._item_ids:
            return []
        q_vec = self.encode_text(f"{query.standard_name} {query.canonical_search_text}")
        sims = self._doc_matrix @ q_vec

        results: list[tuple[str, float]] = []
        for idx, sim in enumerate(sims):
            item_id = self._item_ids[idx]
            item = self._items_map[item_id]
            if ctx.preferred_domains and item.domain not in ctx.preferred_domains:
                continue
            if sim > 0.05:
                results.append((item_id, float(sim)))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_n]

    def status_info(self) -> dict[str, Any]:
        return {
            "backend_kind": self._backend_kind,
            "model_name": self._active_model_name,
            "dim": self._dim,
            "device": self._device if self._backend_kind == "bge" else "cpu",
            "indexed_items": len(self._item_ids),
            "local_bge_m3_ready": _find_local_hf_snapshot("BAAI/bge-m3") is not None,
            "local_bge_base_ready": _find_local_hf_snapshot("BAAI/bge-base-zh-v1.5") is not None,
        }
