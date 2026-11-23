import numpy as np
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import normalize

from src.core.contracts.boq_item import StandardizedBoqItem
from src.core.contracts.matching import MatchContext
from src.core.registry import PluginRegistry


@PluginRegistry.register("retrievers")
class DenseSemanticRetriever:
    """离线稠密语义向量召回插件（Character N-gram + SVD 语义子空间投影，可无缝替换为 BGE-M3）"""

    retriever_id: str = "dense"

    def __init__(self, n_components: int = 128) -> None:
        self.n_components = n_components
        self._vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), lowercase=True)
        self._svd: TruncatedSVD | None = None
        self._doc_matrix: np.ndarray | None = None
        self._item_ids: list[str] = []
        self._items_map: dict[str, StandardizedBoqItem] = {}

    def build_index(self, items: list[StandardizedBoqItem]) -> None:
        self._item_ids = [it.item_id for it in items]
        self._items_map = {it.item_id: it for it in items}
        if not items:
            self._doc_matrix = None
            return

        corpus = [f"{it.standard_name} {it.canonical_search_text}" for it in items]
        tfidf = self._vectorizer.fit_transform(corpus)
        n_comp = min(self.n_components, max(2, tfidf.shape[0] - 1), max(2, tfidf.shape[1] - 1))
        if tfidf.shape[0] >= 3 and tfidf.shape[1] >= 3:
            self._svd = TruncatedSVD(n_components=n_comp, random_state=42)
            dense = self._svd.fit_transform(tfidf)
            self._doc_matrix = normalize(dense, norm="l2")
        else:
            self._svd = None
            self._doc_matrix = normalize(tfidf.toarray(), norm="l2")

    def encode_text(self, text: str) -> np.ndarray:
        vec = self._vectorizer.transform([text])
        if self._svd is not None:
            dense = self._svd.transform(vec)
        else:
            dense = vec.toarray()
        return normalize(dense, norm="l2")[0]

    def similarity(self, text_a: str, text_b: str) -> float:
        if not self._item_ids:
            return 0.0
        va = self.encode_text(text_a)
        vb = self.encode_text(text_b)
        return float(max(0.0, np.dot(va, vb)))

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
