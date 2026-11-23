from src.core.contracts.boq_item import FeatureDimension, StandardizedBoqItem
from src.core.contracts.matching import ExplainAttribution, MatchedCandidate
from src.core.registry import PluginRegistry


@PluginRegistry.register("explainers")
class ShapAhpExplainer:
    """SHAP/AHP 多层次特征归因与定额案例双溯源插件"""

    explainer_id: str = "shap_ahp_explainer"

    def explain(self, query: StandardizedBoqItem, candidate: MatchedCandidate) -> ExplainAttribution:
        comparisons = list(candidate.attribution.slot_comparisons)
        dim_totals: dict[str, float] = {
            FeatureDimension.ENTITY_SPEC.value: 0.0,
            FeatureDimension.PROCESS_WORK.value: 0.0,
            FeatureDimension.ENV_CONDITION.value: 0.0,
        }

        for sc in comparisons:
            q_slot = query.slots.get(sc.slot_key)
            c_slot = candidate.candidate_item.slots.get(sc.slot_key)
            ref = q_slot or c_slot
            if ref:
                dim_totals[ref.dimension.value] += sc.contribution_weight

        dim_totals["semantic_text"] = round(0.22 * candidate.raw_dense_score, 4)
        dim_totals["context_affinity"] = round(0.13 * (1.0 if candidate.final_score > 0.6 else 0.4), 4)

        total_mass = sum(dim_totals.values()) or 1.0
        contributions = {k: round(v / total_mass, 4) for k, v in dim_totals.items()}

        proj = candidate.candidate_project_ctx
        quota_codes = (
            list(candidate.candidate_item.breakdown.quota_codes)
            if candidate.candidate_item.breakdown
            else []
        )

        matched_labels = [
            f"{sc.slot_label}={sc.candidate_value}"
            for sc in comparisons
            if sc.similarity >= 0.85 and not sc.is_hard_conflict
        ]
        conflict_labels = [
            f"{sc.slot_label}(查询:{sc.query_value} vs 历史:{sc.candidate_value})"
            for sc in comparisons
            if sc.is_hard_conflict
        ]

        parts = [f"溯源至【{proj.project_name}】({proj.province}, {proj.price_base_month}, {proj.stage.value})"]
        if quota_codes:
            parts.append(f"定额依据[{','.join(quota_codes)}]")
        elif proj.quota_standards:
            parts.append(f"依据[{proj.quota_standards[0]}]")
        if matched_labels:
            parts.append("核心匹配：" + "、".join(matched_labels[:3]))
        if conflict_labels:
            parts.append("[硬规格差异] " + "；".join(conflict_labels))

        return ExplainAttribution(
            slot_comparisons=comparisons,
            feature_contributions=contributions,
            provenance_project_name=proj.project_name,
            provenance_province=proj.province,
            provenance_month=proj.price_base_month,
            provenance_stage=proj.stage.value,
            provenance_quota_codes=quota_codes,
            summary_reason=" | ".join(parts),
        )
