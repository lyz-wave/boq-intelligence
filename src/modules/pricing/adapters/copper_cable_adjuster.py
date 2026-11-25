from typing import Optional

from src.core.contracts.matching import AdjustedPriceBreakdown, MatchedCandidate
from src.core.contracts.project import MaterialPriceRecord, ProjectContext
from src.core.registry import PluginRegistry
from src.modules.standardizer.unit_converter import unit_conversion_ratio


@PluginRegistry.register("adjusters")
class CopperCableDifferentialAdjuster:
    """L1 铜芯电力/控制电缆物理截面含铜量联动调差算子（基于等效总截面与电解铜信息价）"""

    adjuster_id: str = "copper_cable_diff"
    priority: int = 15
    DEFAULT_BASE_COPPER_YUAN_PER_T: float = 72000.0

    def supports(self, candidate: MatchedCandidate, target_ctx: ProjectContext) -> bool:
        if self._find_copper_price_per_t(target_ctx.base_materials) is None:
            return False
        item = candidate.candidate_item
        if item.category_code == "07_02_cable_termination":
            return False
        name_txt = item.standard_name or ""
        blob = f"{name_txt} {item.raw_text_full}"
        if not any(k in blob for k in ("电缆", "母线", "铜排")):
            return False
        if any(bad in name_txt for bad in ("电缆头", "终端", "铜鼻子")):
            return False
        if any(alu in blob for alu in ("VLV", "YJLV", "铝芯")):
            return False
        if item.standard_unit != "m":
            return False
        sec_slot = item.slots.get("cross_section_eq_mm2")
        return bool(sec_slot and (sec_slot.numeric_value or 0.0) > 0)

    def adjust(
        self,
        candidate: MatchedCandidate,
        target_ctx: ProjectContext,
        target_unit: str | None = None,
    ) -> AdjustedPriceBreakdown:
        item = candidate.candidate_item
        orig_price = item.normalized_unit_price_yuan or 0.0
        target_cu = self._find_copper_price_per_t(target_ctx.base_materials) or self.DEFAULT_BASE_COPPER_YUAN_PER_T
        hist_cu = (
            self._find_copper_price_per_t(candidate.candidate_project_ctx.base_materials)
            or self.DEFAULT_BASE_COPPER_YUAN_PER_T
        )

        area_mm2 = float(item.slots["cross_section_eq_mm2"].numeric_value or 0.0)
        # 铜密度 8.89 g/cm³，考虑绞合紧压与损耗系数 1.025 -> t/m = area_mm2 * 8.89e-6 * 1.025
        copper_t_per_m = area_mm2 * 8.89e-6 * 1.025
        delta_per_m = copper_t_per_m * (target_cu - hist_cu)

        unit_ratio = unit_conversion_ratio(target_unit, item.standard_unit) if target_unit else 1.0
        adj_price = round((orig_price + delta_per_m) * unit_ratio, 4)

        if abs(delta_per_m) < 1e-4:
            details = ["铜价与基准期一致，保留原综合单价"]
        else:
            details = [
                f"铜芯截面材差联动: 截面 {area_mm2:g}mm² (折合含铜 {copper_t_per_m * 1000:.3f}kg/m), "
                f"电解铜 {hist_cu:.0f} -> {target_cu:.0f} 元/t, 单价增减 {delta_per_m:+.2f}元/m"
            ]

        return AdjustedPriceBreakdown(
            adjuster_used=self.adjuster_id,
            original_unit_price_yuan=orig_price,
            adjusted_unit_price_yuan=adj_price,
            adjustment_factor=round(adj_price / orig_price, 4) if orig_price else 1.0,
            price_diff_details=details,
        )

    @staticmethod
    def _find_copper_price_per_t(materials: list[MaterialPriceRecord]) -> Optional[float]:
        for m in materials:
            if m.material_name in ("电解铜", "铜", "铜材", "1#电解铜"):
                val = m.budget_price_excl_tax if m.budget_price_excl_tax is not None else m.base_price
                if val is None or val <= 0:
                    continue
                if m.unit.strip().lower() == "kg":
                    return float(val) * 1000.0
                return float(val)
        return None
