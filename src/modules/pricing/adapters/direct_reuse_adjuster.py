from src.core.contracts.matching import AdjustedPriceBreakdown, MatchedCandidate
from src.core.contracts.project import ProjectContext
from src.core.registry import PluginRegistry
from src.modules.standardizer.unit_converter import unit_conversion_ratio


@PluginRegistry.register("adjusters")
class DirectReuseAdjuster:
    """直接复用与计量单位换算调价算子（兜底算子）"""

    adjuster_id: str = "direct_reuse"
    priority: int = 90

    def supports(self, candidate: MatchedCandidate, target_ctx: ProjectContext) -> bool:
        return candidate.candidate_item.normalized_unit_price_yuan is not None

    def adjust(
        self,
        candidate: MatchedCandidate,
        target_ctx: ProjectContext,
        target_unit: str | None = None,
    ) -> AdjustedPriceBreakdown:
        orig_price = candidate.candidate_item.normalized_unit_price_yuan or 0.0
        src_unit = candidate.candidate_item.standard_unit
        ratio = unit_conversion_ratio(target_unit, src_unit) if target_unit else 1.0
        # 例：历史单价为 元/hm²，目标单位为 亩 (ratio = 666.67 / 10000 = 0.0667) -> 元/亩 = 元/hm² * 0.0667
        adj_price = round(orig_price * ratio, 4)
        details = []
        if ratio != 1.0:
            details.append(f"单位换算 ({src_unit} -> {target_unit}): ×{ratio:.4f}")
        else:
            details.append("同量纲直接复用历史综合单价")
        return AdjustedPriceBreakdown(
            adjuster_used=self.adjuster_id,
            original_unit_price_yuan=orig_price,
            adjusted_unit_price_yuan=adj_price,
            adjustment_factor=round(adj_price / orig_price, 4) if orig_price else 1.0,
            price_diff_details=details,
        )
