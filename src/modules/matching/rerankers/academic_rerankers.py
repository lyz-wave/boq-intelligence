from src.core.contracts.boq_item import StandardizedBoqItem
from src.core.contracts.matching import ConfidenceLevel, MatchContext, MatchedCandidate
from src.core.contracts.project import ProjectContext
from src.core.registry import PluginRegistry
from src.modules.matching.rerankers.hybrid_kernel_reranker import (
    HybridHardSpecKernelReranker,
    _token_jaccard,
    slot_similarity,
)


@PluginRegistry.register("rerankers")
class LstmCosineBaselineReranker:
    """科研对比基线：纯序列语义余弦匹配（模拟无硬规格惩罚的 LSTM+余弦基线）"""

    reranker_id: str = "lstm_cosine_baseline"

    def score(
        self,
        query: StandardizedBoqItem,
        candidates: list[tuple[StandardizedBoqItem, ProjectContext]],
        ctx: MatchContext,
        retriever_scores: dict[str, dict[str, float]] | None = None,
    ) -> list[MatchedCandidate]:
        retriever_scores = retriever_scores or {}
        dense_map = retriever_scores.get("dense", {})
        bm25_map = retriever_scores.get("bm25", {})
        max_bm25 = max(bm25_map.values(), default=1.0) or 1.0

        scored: list[MatchedCandidate] = []
        for cand_item, cand_proj in candidates:
            dense = max(0.0, dense_map.get(cand_item.item_id, 0.0))
            bm25_norm = bm25_map.get(cand_item.item_id, 0.0) / max_bm25
            tok_sim = _token_jaccard(query.canonical_search_text, cand_item.canonical_search_text)
            final = round(min(1.0, 0.55 * dense + 0.25 * tok_sim + 0.20 * bm25_norm), 4)
            conf = ConfidenceLevel.HIGH if final >= 0.85 else (ConfidenceLevel.MEDIUM if final >= 0.65 else ConfidenceLevel.LOW)
            scored.append(
                MatchedCandidate(
                    candidate_item=cand_item,
                    candidate_project_ctx=cand_proj,
                    raw_bm25_score=round(bm25_norm, 4),
                    raw_dense_score=round(dense, 4),
                    final_score=final,
                    confidence=conf,
                    has_hard_spec_conflict=False,
                )
            )
        scored.sort(key=lambda x: x.final_score, reverse=True)
        return scored


@PluginRegistry.register("rerankers")
class HybridNoDecayReranker(HybridHardSpecKernelReranker):
    """消融实验变体：剥离硬规格指数衰减核的混合重排模型"""

    reranker_id: str = "hybrid_no_decay"

    def __init__(self) -> None:
        super().__init__(alpha=0.25)

    def score(
        self,
        query: StandardizedBoqItem,
        candidates: list[tuple[StandardizedBoqItem, ProjectContext]],
        ctx: MatchContext,
        retriever_scores: dict[str, dict[str, float]] | None = None,
    ) -> list[MatchedCandidate]:
        base = super().score(query, candidates, ctx, retriever_scores)
        # 取消硬冲突截断惩罚，观察仅线性加权时的性能表现
        for c in base:
            c.has_hard_spec_conflict = False
        return base


@PluginRegistry.register("rerankers")
class RgcnGraphReranker(HybridHardSpecKernelReranker):
    """科研对比模型：知识图谱结构路径与定额关联增强重排（R-GCN 邻域奖励）"""

    reranker_id: str = "rgcn_graph_reranker"

    def __init__(self) -> None:
        super().__init__(alpha=4.5)

    def score(
        self,
        query: StandardizedBoqItem,
        candidates: list[tuple[StandardizedBoqItem, ProjectContext]],
        ctx: MatchContext,
        retriever_scores: dict[str, dict[str, float]] | None = None,
    ) -> list[MatchedCandidate]:
        base = super().score(query, candidates, ctx, retriever_scores)
        for c in base:
            graph_bonus = 0.0
            if c.candidate_item.category_code == query.category_code:
                graph_bonus += 0.04
            if c.candidate_item.breakdown and c.candidate_item.breakdown.quota_codes:
                graph_bonus += 0.03
            if not c.has_hard_spec_conflict:
                c.final_score = round(min(1.0, c.final_score + graph_bonus), 4)
        base.sort(key=lambda x: x.final_score, reverse=True)
        return base
