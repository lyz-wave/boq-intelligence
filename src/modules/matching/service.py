from pathlib import Path
from typing import Any, Optional

import yaml

from src.core.contracts.boq_item import StandardizedBoqItem
from src.core.contracts.matching import ConfidenceLevel, MatchContext, MatchedCandidate, MatchResult
from src.core.contracts.project import ProjectContext
from src.core.ports.matcher_port import ExplainerPort, RerankerPort, RetrieverPort
from src.core.registry import PluginRegistry
from src.modules.matching import explainers as _loaded_explainers  # noqa: F401
from src.modules.matching import rerankers as _loaded_rerankers  # noqa: F401
from src.modules.matching import retrievers as _loaded_retrievers  # noqa: F401
from src.modules.pricing.service import PriceAdjustmentModule
from src.modules.standardizer.unit_converter import units_compatible


class PriceMatchingModule:
    """混合智能价格匹配深模块：对外仅暴露 build_index() 与 match(query, ctx) -> MatchResult."""

    def __init__(
        self,
        pipelines_cfg_path: Optional[Path] = None,
        pricing_module: Optional[PriceAdjustmentModule] = None,
    ) -> None:
        cfg_path = pipelines_cfg_path or Path(__file__).resolve().parents[3] / "configs" / "pipelines.yaml"
        self._pipelines_cfg: dict[str, Any] = {}
        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as f:
                self._pipelines_cfg = (yaml.safe_load(f) or {}).get("matching_pipelines", {})

        self._pricing = pricing_module or PriceAdjustmentModule()
        self._items_map: dict[str, StandardizedBoqItem] = {}
        self._projects_map: dict[str, ProjectContext] = {}
        self._retrievers: dict[str, RetrieverPort] = {
            rid: cls() for rid, cls in PluginRegistry.all_in("retrievers").items()
        }
        self._rerankers: dict[str, RerankerPort] = {
            rid: cls() for rid, cls in PluginRegistry.all_in("rerankers").items()
        }
        self._explainers: dict[str, ExplainerPort] = {
            eid: cls() for eid, cls in PluginRegistry.all_in("explainers").items()
        }

    def build_index(
        self, items: list[StandardizedBoqItem], projects: list[ProjectContext]
    ) -> None:
        self._items_map = {it.item_id: it for it in items}
        self._projects_map = {p.project_id: p for p in projects}
        for ret in self._retrievers.values():
            ret.build_index(items)

    def match(self, query: StandardizedBoqItem, ctx: MatchContext) -> MatchResult:
        if not self._items_map:
            return MatchResult(
                query_item_id=query.item_id,
                confidence=ConfidenceLevel.LOW,
                warning_messages=["历史价格库为空，无法执行匹配"],
            )

        pipe_cfg = self._pipelines_cfg.get(
            ctx.pipeline_name,
            {
                "retrievers": ["bm25", "dense", "slot_exact"],
                "reranker": "hybrid_hard_spec_kernel",
                "explainer": "shap_ahp_explainer",
                "rrf_k": 60,
                "top_pool": 50,
            },
        )

        rrf_k = int(pipe_cfg.get("rrf_k", 60))
        top_pool = int(pipe_cfg.get("top_pool", 50))
        ret_scores: dict[str, dict[str, float]] = {}
        rrf_totals: dict[str, float] = {}

        for rid in pipe_cfg.get("retrievers", ["bm25", "dense"]):
            ret = self._retrievers.get(rid)
            if not ret:
                continue
            hits = ret.retrieve(query, ctx, top_n=top_pool)
            ret_scores[rid] = {item_id: score for item_id, score in hits}
            for rank, (item_id, _) in enumerate(hits, start=1):
                rrf_totals[item_id] = rrf_totals.get(item_id, 0.0) + 1.0 / (rrf_k + rank)

        # 第一阶段硬过滤：优先保留计量单位兼容族；若完全无结果则放开退避
        ranked_ids = [iid for iid, _ in sorted(rrf_totals.items(), key=lambda x: x[1], reverse=True)]
        compatible_ids = [
            iid
            for iid in ranked_ids
            if iid in self._items_map and units_compatible(query.standard_unit, self._items_map[iid].standard_unit)
        ]
        pool_ids = (compatible_ids or ranked_ids)[:top_pool]

        candidate_pairs: list[tuple[StandardizedBoqItem, ProjectContext]] = []
        for iid in pool_ids:
            c_item = self._items_map.get(iid)
            if not c_item:
                continue
            c_proj = self._projects_map.get(c_item.project_id)
            if not c_proj:
                continue
            candidate_pairs.append((c_item, c_proj))

        if not candidate_pairs:
            return MatchResult(
                query_item_id=query.item_id,
                confidence=ConfidenceLevel.LOW,
                warning_messages=["未检索到相似历史清单，建议人工询价或补充套定额"],
            )

        reranker_id = pipe_cfg.get("reranker", "hybrid_hard_spec_kernel")
        reranker = self._rerankers.get(reranker_id) or next(iter(self._rerankers.values()))
        reranked: list[MatchedCandidate] = reranker.score(query, candidate_pairs, ctx, ret_scores)[: ctx.top_k]

        explainer_id = pipe_cfg.get("explainer", "shap_ahp_explainer")
        explainer = self._explainers.get(explainer_id)
        if explainer:
            for cand in reranked:
                cand.attribution = explainer.explain(query, cand)

        best = reranked[0]
        warnings: list[str] = []
        if best.has_hard_spec_conflict:
            warnings.append("检测到硬约束规格冲突，推荐单价仅供参考，请务必人工核实规格差异。")

        adjusted = None
        recommended_price = best.candidate_item.normalized_unit_price_yuan
        if ctx.auto_adjust_price and ctx.target_project_ctx and recommended_price is not None:
            adjusted = self._pricing.adjust(best, ctx.target_project_ctx, target_unit=query.standard_unit)
            recommended_price = adjusted.adjusted_unit_price_yuan

        return MatchResult(
            query_item_id=query.item_id,
            recommended_unit_price_yuan=recommended_price,
            confidence=best.confidence,
            candidates=reranked,
            adjusted_price=adjusted,
            warning_messages=warnings,
        )
