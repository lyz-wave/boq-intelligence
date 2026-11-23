import math
import re

from src.core.contracts.boq_item import FeatureSlotValue, StandardizedBoqItem
from src.core.contracts.matching import (
    ConfidenceLevel,
    ExplainAttribution,
    MatchContext,
    MatchedCandidate,
    SlotComparison,
)
from src.core.contracts.project import ProjectContext
from src.core.registry import PluginRegistry
from src.modules.matching.tokenizer import tokenize_boq_text
from src.modules.standardizer.unit_converter import units_compatible


def _token_jaccard(text_a: str, text_b: str) -> float:
    ta = set(tokenize_boq_text(text_a))
    tb = set(tokenize_boq_text(text_b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _extract_year(month_str: str) -> int:
    m = re.match(r"(\d{4})", month_str or "")
    return int(m.group(1)) if m else 2026


def slot_similarity(
    q_slot: FeatureSlotValue | None,
    c_slot: FeatureSlotValue | None,
    alpha: float = 4.5,
) -> tuple[float, bool]:
    """计算单个特征槽位的匹配相似度，返回 (similarity, is_hard_conflict)."""
    if q_slot is None and c_slot is None:
        return 1.0, False
    if q_slot is not None and c_slot is None:
        return (0.30, False) if q_slot.hard_constraint else (0.50, False)
    if q_slot is None and c_slot is not None:
        return (0.75, False) if c_slot.hard_constraint else (0.85, False)

    assert q_slot is not None and c_slot is not None
    # 1. 数值型硬约束槽位：应用硬规格指数衰减核
    if q_slot.numeric_value is not None and c_slot.numeric_value is not None:
        xq = q_slot.numeric_value
        xc = c_slot.numeric_value
        denom = max(abs(xq), abs(xc), 1e-6)
        rel_diff = abs(xq - xc) / denom
        decay = math.exp(-alpha * rel_diff) if q_slot.hard_constraint else math.exp(-1.5 * rel_diff)
        is_conflict = bool(q_slot.hard_constraint and rel_diff > 0.30)
        return round(decay, 4), is_conflict

    # 2. 枚举/字符型槽位
    vq = str(q_slot.normalized_value).strip().upper()
    vc = str(c_slot.normalized_value).strip().upper()
    if vq == vc:
        return 1.0, False
    if vq in vc or vc in vq:
        # 例如 ZR-YJV 与 YJV 为近义族，但 户内 vs 户外 不相交
        return 0.72, False
    if q_slot.hard_constraint:
        # 如 电力电缆 vs 控制电缆, 户内 vs 户外, 黑沙蒿 vs 北沙柳
        return 0.0, True
    return round(_token_jaccard(vq, vc), 4), False


@PluginRegistry.register("rerankers")
class HybridHardSpecKernelReranker:
    """生产默认重排器：硬规格指数衰减核 + AHP 特征权重 + L0 上下文亲和"""

    reranker_id: str = "hybrid_hard_spec_kernel"

    def __init__(self, alpha: float = 4.5) -> None:
        self.alpha = alpha

    def score(
        self,
        query: StandardizedBoqItem,
        candidates: list[tuple[StandardizedBoqItem, ProjectContext]],
        ctx: MatchContext,
        retriever_scores: dict[str, dict[str, float]] | None = None,
    ) -> list[MatchedCandidate]:
        retriever_scores = retriever_scores or {}
        bm25_map = retriever_scores.get("bm25", {})
        dense_map = retriever_scores.get("dense", {})
        slot_map = retriever_scores.get("slot_exact", {})

        max_bm25 = max(bm25_map.values(), default=1.0) or 1.0
        max_slot = max(slot_map.values(), default=1.0) or 1.0

        scored: list[MatchedCandidate] = []
        for cand_item, cand_proj in candidates:
            bm25_norm = min(1.0, bm25_map.get(cand_item.item_id, 0.0) / max_bm25)
            dense_score = min(1.0, max(0.0, dense_map.get(cand_item.item_id, 0.0)))
            slot_ret_norm = min(1.0, slot_map.get(cand_item.item_id, 0.0) / max_slot)

            all_keys = list(dict.fromkeys([*query.slots.keys(), *cand_item.slots.keys()]))
            slot_comparisons: list[SlotComparison] = []
            hard_kernel_scores: list[float] = []
            has_conflict = False
            weighted_sum = 0.0
            weight_total = 0.0

            for k in all_keys:
                qs = query.slots.get(k)
                cs = cand_item.slots.get(k)
                sim, conflict = slot_similarity(qs, cs, alpha=self.alpha)
                if conflict:
                    has_conflict = True
                ref = qs or cs
                assert ref is not None
                w = ref.ahp_weight or 0.15
                if ref.hard_constraint and qs is not None:
                    hard_kernel_scores.append(sim)
                # 当仅候选有槽位而查询未写全时，降低其分母占权重以免过度惩罚短查询
                eff_w = w if qs is not None else w * 0.35
                weighted_sum += eff_w * sim
                weight_total += eff_w

                slot_comparisons.append(
                    SlotComparison(
                        slot_key=k,
                        slot_label=ref.slot_label,
                        query_value=qs.normalized_value if qs else None,
                        candidate_value=cs.normalized_value if cs else None,
                        similarity=sim,
                        contribution_weight=round(eff_w * sim, 4),
                        is_hard_conflict=conflict,
                    )
                )

            # 单位族硬约束校验
            unit_ok = units_compatible(query.standard_unit, cand_item.standard_unit)
            if not unit_ok:
                has_conflict = True

            name_sim = self._name_similarity(query.standard_name, cand_item.standard_name)
            text_overlap = _token_jaccard(query.raw_text_full, cand_item.raw_text_full)
            if weight_total > 0:
                feat_score = 0.80 * (weighted_sum / weight_total) + 0.20 * name_sim
            else:
                feat_score = 0.55 * name_sim + 0.45 * text_overlap

            sem_score = 0.55 * dense_score + 0.30 * bm25_norm + 0.15 * max(slot_ret_norm, text_overlap)
            ctx_score = self._context_affinity(query, cand_item, cand_proj, ctx)

            hard_kernel_min = min(hard_kernel_scores) if hard_kernel_scores else 1.0
            final_score = 0.65 * feat_score + 0.22 * sem_score + 0.13 * ctx_score

            # 硬规格冲突阻断与惩罚
            if has_conflict:
                penalty = min(hard_kernel_min, 0.55) if unit_ok else 0.25
                final_score = min(final_score * (0.45 + 0.55 * penalty), 0.58)
            elif not query.slots and name_sim >= 0.85:
                final_score = max(final_score, 0.86)

            final_score = round(min(1.0, max(0.0, final_score)), 4)

            if has_conflict or final_score < 0.65:
                conf = ConfidenceLevel.LOW
            elif final_score >= 0.85:
                conf = ConfidenceLevel.HIGH
            else:
                conf = ConfidenceLevel.MEDIUM

            scored.append(
                MatchedCandidate(
                    candidate_item=cand_item,
                    candidate_project_ctx=cand_proj,
                    raw_bm25_score=round(bm25_norm, 4),
                    raw_dense_score=round(dense_score, 4),
                    raw_slot_score=round(feat_score, 4),
                    hard_spec_kernel_score=round(hard_kernel_min, 4),
                    final_score=final_score,
                    confidence=conf,
                    has_hard_spec_conflict=has_conflict,
                    attribution=ExplainAttribution(slot_comparisons=slot_comparisons),
                )
            )

        scored.sort(key=lambda c: c.final_score, reverse=True)
        return scored

    @staticmethod
    def _name_similarity(q_name: str, c_name: str) -> float:
        if not q_name or not c_name:
            return 0.0
        if q_name == c_name:
            return 1.0
        if q_name in c_name or c_name in q_name:
            return 0.82
        return _token_jaccard(q_name, c_name)

    @staticmethod
    def _context_affinity(
        query: StandardizedBoqItem,
        cand_item: StandardizedBoqItem,
        cand_proj: ProjectContext,
        ctx: MatchContext,
    ) -> float:
        score = 0.0
        target_provs = list(ctx.preferred_provinces)
        if ctx.target_project_ctx and ctx.target_project_ctx.province and ctx.target_project_ctx.province != "未指定":
            target_provs.append(ctx.target_project_ctx.province)

        if target_provs:
            if any(p in cand_proj.province or cand_proj.province in p for p in target_provs):
                score += 0.50
        else:
            score += 0.35

        if ctx.target_project_ctx:
            if ctx.target_project_ctx.stage == cand_proj.stage:
                score += 0.25
            y_q = _extract_year(ctx.target_project_ctx.price_base_month)
            y_c = _extract_year(cand_proj.price_base_month)
            score += 0.25 * math.exp(-0.15 * abs(y_q - y_c))
        else:
            if query.domain == cand_item.domain:
                score += 0.30
            score += 0.20
        return min(1.0, score)
