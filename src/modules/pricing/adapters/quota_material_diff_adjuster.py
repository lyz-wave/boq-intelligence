import re
from typing import Optional

from src.core.contracts.matching import AdjustedPriceBreakdown, MatchedCandidate
from src.core.contracts.project import MaterialPriceRecord, ProjectContext
from src.core.registry import PluginRegistry
from src.modules.standardizer.unit_converter import unit_conversion_ratio


def _to_unit_price(rec: MaterialPriceRecord, target_unit: str) -> Optional[float]:
    val = rec.budget_price_excl_tax if rec.budget_price_excl_tax is not None else rec.base_price
    if val is None:
        return None
    src_u = rec.unit.strip()
    dst_u = target_unit.strip()
    if src_u == dst_u:
        return val
    if src_u == "t" and dst_u == "kg":
        return val / 1000.0
    if src_u == "kg" and dst_u == "t":
        return val * 1000.0
    return val


@PluginRegistry.register("adjusters")
class QuotaMaterialDiffAdjuster:
    """基于 L2 定额工料机消耗量的主材价差动态重算算子"""

    adjuster_id: str = "quota_material_diff"
    priority: int = 10

    def supports(self, candidate: MatchedCandidate, target_ctx: ProjectContext) -> bool:
        bd = candidate.candidate_item.breakdown
        return bool(bd and bd.resource_lines and (target_ctx.base_materials or target_ctx.labor_rates))

    def adjust(
        self,
        candidate: MatchedCandidate,
        target_ctx: ProjectContext,
        target_unit: str | None = None,
    ) -> AdjustedPriceBreakdown:
        item = candidate.candidate_item
        orig_price = item.normalized_unit_price_yuan or 0.0
        bd = item.breakdown
        assert bd is not None

        quota_factor = self._extract_quota_unit_factor(bd.quota_unit, item.standard_unit)
        target_mat_map = {m.material_name: m for m in target_ctx.base_materials}
        hist_mat_map = {m.material_name: m for m in candidate.candidate_project_ctx.base_materials}

        delta_quota_yuan = 0.0
        details: list[str] = []

        for line in bd.resource_lines:
            if line.resource_type == "价差" and line.quantity > 0:
                target_mat = self._find_material(line.name, target_mat_map)
                if not target_mat:
                    continue
                new_mat_price = _to_unit_price(target_mat, line.unit)
                if new_mat_price is None:
                    continue

                hist_mat = self._find_material(line.name, hist_mat_map)
                old_mat_price = _to_unit_price(hist_mat, line.unit) if hist_mat else None
                base_price = hist_mat.base_price if (hist_mat and hist_mat.base_price is not None) else None
                if hist_mat and base_price is not None and hist_mat.unit == "t" and line.unit == "kg":
                    base_price = base_price / 1000.0

                if old_mat_price is not None:
                    diff_per_unit = new_mat_price - old_mat_price
                elif base_price is not None:
                    old_mat_price = base_price + line.unit_price
                    diff_per_unit = new_mat_price - old_mat_price
                else:
                    old_mat_price = line.unit_price
                    diff_per_unit = new_mat_price - old_mat_price

                if abs(diff_per_unit) > 1e-4:
                    delta_line = line.quantity * diff_per_unit
                    delta_quota_yuan += delta_line
                    details.append(
                        f"主材[{line.name}]价差替换: {old_mat_price:.2f} -> {new_mat_price:.2f} 元/{line.unit} "
                        f"(消耗 {line.quantity}{line.unit}/{bd.quota_unit or ''}, 增减 {delta_line:+.2f}元)"
                    )

        # 若未调整到任何主材，则回退直接复用
        tax_mult = 1.0 + ((bd.tax_rate_pct or 9.0) / 100.0)
        delta_per_std_unit = (delta_quota_yuan * tax_mult) / quota_factor
        unit_ratio = unit_conversion_ratio(target_unit, item.standard_unit) if target_unit else 1.0
        adj_price = round((orig_price + delta_per_std_unit) * unit_ratio, 4)

        if not details:
            details.append("L2 定额工料机基准一致，保留原综合单价")

        return AdjustedPriceBreakdown(
            adjuster_used=self.adjuster_id,
            original_unit_price_yuan=orig_price,
            adjusted_unit_price_yuan=adj_price,
            adjustment_factor=round(adj_price / orig_price, 4) if orig_price else 1.0,
            price_diff_details=details,
        )

    @staticmethod
    def _find_material(name: str, mat_map: dict[str, MaterialPriceRecord]) -> Optional[MaterialPriceRecord]:
        if name in mat_map:
            return mat_map[name]
        for k, v in mat_map.items():
            if name in k or k in name:
                return v
        return None

    @staticmethod
    def _extract_quota_unit_factor(quota_unit: Optional[str], std_unit: Optional[str]) -> float:
        if not quota_unit:
            return 1.0
        m = re.match(r"(\d+)", quota_unit.strip())
        return float(m.group(1)) if m else 1.0
