from src.core.contracts.boq_item import StandardizedBoqItem
from src.core.contracts.matching import MatchContext
from src.core.registry import PluginRegistry


@PluginRegistry.register("retrievers")
class SlotExactRetriever:
    """结构化槽位与标准名检索插件"""

    retriever_id: str = "slot_exact"

    def __init__(self) -> None:
        self._items: list[StandardizedBoqItem] = []

    def build_index(self, items: list[StandardizedBoqItem]) -> None:
        self._items = list(items)

    def retrieve(self, query: StandardizedBoqItem, ctx: MatchContext, top_n: int) -> list[tuple[str, float]]:
        results: list[tuple[str, float]] = []
        for it in self._items:
            if ctx.preferred_domains and it.domain not in ctx.preferred_domains:
                continue
            score = 0.0
            if it.category_code == query.category_code:
                score += 0.4
            if it.standard_name and query.standard_name:
                if it.standard_name == query.standard_name:
                    score += 0.6
                elif it.standard_name in query.standard_name or query.standard_name in it.standard_name:
                    score += 0.35
            shared_keys = set(query.slots.keys()) & set(it.slots.keys())
            for k in shared_keys:
                if str(query.slots[k].normalized_value) == str(it.slots[k].normalized_value):
                    score += 0.3
            if score > 0:
                results.append((it.item_id, score))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_n]
