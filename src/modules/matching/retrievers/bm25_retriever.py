from collections import Counter
import math

from src.core.contracts.boq_item import StandardizedBoqItem
from src.core.contracts.matching import MatchContext
from src.core.registry import PluginRegistry
from src.modules.matching.tokenizer import tokenize_boq_text


@PluginRegistry.register("retrievers")
class Bm25Retriever:
    """稀疏词权召回插件 (Okapi BM25)"""

    retriever_id: str = "bm25"

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._item_ids: list[str] = []
        self._doc_tf: list[Counter[str]] = []
        self._doc_len: list[int] = []
        self._avgdl: float = 1.0
        self._idf: dict[str, float] = {}
        self._items_map: dict[str, StandardizedBoqItem] = {}

    def build_index(self, items: list[StandardizedBoqItem]) -> None:
        self._item_ids = []
        self._doc_tf = []
        self._doc_len = []
        self._items_map = {it.item_id: it for it in items}

        df: Counter[str] = Counter()
        for it in items:
            tokens = tokenize_boq_text(f"{it.standard_name} {it.canonical_search_text}")
            tf = Counter(tokens)
            self._item_ids.append(it.item_id)
            self._doc_tf.append(tf)
            self._doc_len.append(len(tokens))
            for tok in tf:
                df[tok] += 1

        n_docs = len(items)
        self._avgdl = (sum(self._doc_len) / n_docs) if n_docs else 1.0
        self._idf = {
            tok: math.log(1.0 + (n_docs - freq + 0.5) / (freq + 0.5))
            for tok, freq in df.items()
        }

    def retrieve(self, query: StandardizedBoqItem, ctx: MatchContext, top_n: int) -> list[tuple[str, float]]:
        if not self._item_ids:
            return []
        q_tokens = tokenize_boq_text(f"{query.standard_name} {query.canonical_search_text}")
        scores: list[tuple[str, float]] = []

        for idx, item_id in enumerate(self._item_ids):
            item = self._items_map[item_id]
            if ctx.preferred_domains and item.domain not in ctx.preferred_domains:
                continue
            tf = self._doc_tf[idx]
            dl = self._doc_len[idx] or 1
            s = 0.0
            for tok in q_tokens:
                if tok not in tf:
                    continue
                idf = self._idf.get(tok, 0.0)
                freq = tf[tok]
                denom = freq + self.k1 * (1.0 - self.b + self.b * (dl / self._avgdl))
                s += idf * (freq * (self.k1 + 1.0) / denom)
            if s > 0:
                scores.append((item_id, s))

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_n]
