from typing import Protocol, runtime_checkable

from src.core.contracts.boq_item import StandardizedBoqItem
from src.core.contracts.matching import ExplainAttribution, MatchContext, MatchedCandidate
from src.core.contracts.project import ProjectContext


@runtime_checkable
class RetrieverPort(Protocol):
    """接缝 3A：多路召回插件接口 (BM25 / Dense / Slot)"""

    retriever_id: str

    def build_index(self, items: list[StandardizedBoqItem]) -> None:
        ...

    def retrieve(self, query: StandardizedBoqItem, ctx: MatchContext, top_n: int) -> list[tuple[str, float]]:
        ...


@runtime_checkable
class RerankerPort(Protocol):
    """接缝 3B：重排打分插件接口 (HybridKernel / LSTM / R-GCN)"""

    reranker_id: str

    def score(
        self,
        query: StandardizedBoqItem,
        candidates: list[tuple[StandardizedBoqItem, ProjectContext]],
        ctx: MatchContext,
        retriever_scores: dict[str, dict[str, float]] | None = None,
    ) -> list[MatchedCandidate]:
        ...


@runtime_checkable
class ExplainerPort(Protocol):
    """接缝 3C：可解释归因插件接口"""

    explainer_id: str

    def explain(self, query: StandardizedBoqItem, candidate: MatchedCandidate) -> ExplainAttribution:
        ...
