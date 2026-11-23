import re

from src.core.contracts.matching import AdjustedPriceBreakdown, MatchedCandidate
from src.core.contracts.project import ProjectContext
from src.core.registry import PluginRegistry
from src.modules.standardizer.unit_converter import unit_conversion_ratio

PROVINCE_COST_INDEX: dict[str, float] = {
    "广东省": 1.08,
    "北京市": 1.12,
    "上海市": 1.12,
    "浙江省": 1.06,
    "江苏省": 1.05,
    "陕西省": 0.96,
    "内蒙古自治区": 0.98,
    "四川省": 0.99,
    "云南省": 0.97,
}


@PluginRegistry.register("adjusters")
class RegionalTimeIndexAdjuster:
    """跨地区造价指数与跨期时间指数平滑修正算子 (ES-CBR)"""

    adjuster_id: str = "regional_index"
    priority: int = 30

    def supports(self, candidate: MatchedCandidate, target_ctx: ProjectContext) -> bool:
        c_proj = candidate.candidate_project_ctx
        if target_ctx.province == "未指定" or c_proj.province == "未指定":
            return False
        diff_prov = target_ctx.province != c_proj.province
        diff_year = self._year(target_ctx.price_base_month) != self._year(c_proj.price_base_month)
        return diff_prov or diff_year

    def adjust(
        self,
        candidate: MatchedCandidate,
        target_ctx: ProjectContext,
        target_unit: str | None = None,
    ) -> AdjustedPriceBreakdown:
        item = candidate.candidate_item
        c_proj = candidate.candidate_project_ctx
        orig_price = item.normalized_unit_price_yuan or 0.0

        src_idx = PROVINCE_COST_INDEX.get(c_proj.province, 1.0)
        dst_idx = PROVINCE_COST_INDEX.get(target_ctx.province, 1.0)
        region_factor = dst_idx / src_idx

        year_diff = self._year(target_ctx.price_base_month) - self._year(c_proj.price_base_month)
        time_factor = (1.0 + 0.022) ** year_diff  # 年均 2.2% 造价指数平滑

        unit_ratio = unit_conversion_ratio(target_unit, item.standard_unit) if target_unit else 1.0
        total_factor = region_factor * time_factor * unit_ratio
        adj_price = round(orig_price * total_factor, 4)

        details = [
            f"地区指数修正 ({c_proj.province} {src_idx:.2f} -> {target_ctx.province} {dst_idx:.2f}): ×{region_factor:.4f}",
            f"时间指数修正 ({c_proj.price_base_month} -> {target_ctx.price_base_month}): ×{time_factor:.4f}",
        ]
        if unit_ratio != 1.0:
            details.append(f"单位换算 ({item.standard_unit} -> {target_unit}): ×{unit_ratio:.4f}")

        return AdjustedPriceBreakdown(
            adjuster_used=self.adjuster_id,
            original_unit_price_yuan=orig_price,
            adjusted_unit_price_yuan=adj_price,
            adjustment_factor=round(total_factor, 4),
            price_diff_details=details,
        )

    @staticmethod
    def _year(month_str: str) -> int:
        m = re.match(r"(\d{4})", month_str or "")
        return int(m.group(1)) if m else 2026
