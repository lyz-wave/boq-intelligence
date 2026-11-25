import os
from typing import Any, Optional

from src.core.contracts.boq_item import StandardizedBoqItem
from src.core.contracts.matching import (
    ConfidenceLevel,
    ExplainAttribution,
    MatchContext,
    MatchedCandidate,
)
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
    """可训练异构知识图谱 2 层 R-GCN 诱导式结构增强重排器（支持检查点加载与折叠热插拔）"""

    reranker_id: str = "rgcn_graph_reranker"

    def __init__(self, predictor: Optional[Any] = None) -> None:
        super().__init__(alpha=4.5)
        self._predictor: Optional[Any] = predictor
        self._dense_encoder: Optional[Any] = None
        if self._predictor is None and not os.environ.get("PYTEST_CURRENT_TEST"):
            self.reload_checkpoint()

    def set_predictor(self, predictor: Optional[Any], dense_encoder: Optional[Any] = None) -> None:
        self._predictor = predictor
        if dense_encoder is not None:
            self._dense_encoder = dense_encoder
        if predictor is not None:
            self.alpha = max(3.5, float(getattr(predictor, "learned_alpha", 4.5)))

    def reload_checkpoint(self) -> bool:
        from src.modules.research.rgcn_trainer import DEFAULT_RGCN_CKPT_PATH, TrainedRgcnPredictor

        pred = TrainedRgcnPredictor.load(DEFAULT_RGCN_CKPT_PATH)
        if pred is not None:
            self.set_predictor(pred)
            return True
        return False

    def score(
        self,
        query: StandardizedBoqItem,
        candidates: list[tuple[StandardizedBoqItem, ProjectContext]],
        ctx: MatchContext,
        retriever_scores: dict[str, dict[str, float]] | None = None,
    ) -> list[MatchedCandidate]:
        pred = self._predictor
        if pred is not None:
            self.alpha = max(3.5, float(getattr(pred, "learned_alpha", 4.5)))

        base = super().score(query, candidates, ctx, retriever_scores)
        if pred is None:
            # 无训练权重时安全降级为同类别/有L2定额图谱关联轻量奖励
            for c in base:
                graph_bonus = 0.0
                if c.candidate_item.category_code == query.category_code:
                    graph_bonus += 0.03
                if c.candidate_item.breakdown and c.candidate_item.breakdown.quota_codes:
                    graph_bonus += 0.02
                if not c.has_hard_spec_conflict:
                    c.final_score = round(min(1.0, c.final_score + graph_bonus), 4)
            base.sort(key=lambda x: x.final_score, reverse=True)
            return base

        # 真实 2 层 PyTorch R-GCN 诱导式图嵌入推理
        q_dense_vec = None
        if self._dense_encoder is not None and hasattr(self._dense_encoder, "encode_text"):
            try:
                q_dense_vec = self._dense_encoder.encode_text(
                    f"{query.standard_name} {query.canonical_search_text}"
                )
            except Exception:
                q_dense_vec = None

        prov = ctx.target_project_ctx.province if ctx.target_project_ctx else "未指定"
        q_rgcn_vec = pred.encode_inductive_query(query, q_dense_vec, province=prov)

        for c in base:
            g_sim = pred.graph_similarity(q_rgcn_vec, c.candidate_item.item_id)
            if c.has_hard_spec_conflict:
                # 维持硬规格衰减核与红灯拦截，绝不因图结构近似而放行跨档错配
                c.final_score = round(min(c.final_score, 0.56), 4)
            else:
                blended = 0.84 * c.final_score + 0.16 * g_sim
                if blended >= 0.85:
                    c.confidence = ConfidenceLevel.HIGH
                elif blended >= 0.65:
                    c.confidence = ConfidenceLevel.MEDIUM
                c.final_score = round(min(1.0, max(0.0, blended)), 4)

        base.sort(key=lambda x: x.final_score, reverse=True)
        return base
