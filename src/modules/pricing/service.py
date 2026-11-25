from typing import Optional

from src.core.contracts.matching import AdjustedPriceBreakdown, MatchedCandidate
from src.core.contracts.project import ProjectContext
from src.core.ports.adjuster_port import PriceAdjusterPort
from src.core.registry import PluginRegistry
from src.modules.pricing import adapters as _loaded_adjusters  # noqa: F401


class PriceAdjustmentModule:
    """动态调价深模块：对外仅暴露 adjust(candidate, target_ctx) -> AdjustedPriceBreakdown 接口."""

    def __init__(self, adjusters: Optional[list[PriceAdjusterPort]] = None) -> None:
        self._adjusters = adjusters or PluginRegistry.instantiate_all("adjusters")

    def adjust(
        self,
        candidate: MatchedCandidate,
        target_ctx: ProjectContext,
        target_unit: Optional[str] = None,
    ) -> AdjustedPriceBreakdown:
        for adj in self._adjusters:
            if adj.supports(candidate, target_ctx):
                res = adj.adjust(candidate, target_ctx, target_unit=target_unit)
                # 若 L2 或铜芯算子未找到价差变动且跨省/跨期，允许进一步检查地区指数算子
                if adj.adjuster_id in ("quota_material_diff", "copper_cable_diff") and "保留原综合单价" in " ".join(res.price_diff_details):
                    continue
                return res
        orig = candidate.candidate_item.normalized_unit_price_yuan or 0.0
        return AdjustedPriceBreakdown(
            adjuster_used="passthrough",
            original_unit_price_yuan=orig,
            adjusted_unit_price_yuan=orig,
            adjustment_factor=1.0,
            price_diff_details=["沿用原综合单价"],
        )
