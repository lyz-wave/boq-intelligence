import re
from typing import Optional

from src.core.contracts.matching import AdjustedPriceBreakdown, MatchedCandidate
from src.core.contracts.project import MaterialPriceRecord, ProjectContext
from src.core.registry import PluginRegistry
from src.modules.standardizer.unit_converter import unit_conversion_ratio

_UNIT_ALIASES: dict[str, str] = {
    "m3": "m³",
    "立方米": "m³",
    "m2": "m²",
    "平米": "m²",
    "平方米": "m²",
    "吨": "t",
    "千克": "kg",
    "公斤": "kg",
    "米": "m",
}

_MATERIAL_ALIAS_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("柴油", ("柴油", "0#柴油", "0号柴油")),
    ("汽油", ("汽油", "92#汽油", "95#汽油")),
    ("钢筋", ("钢筋", "螺纹钢", "圆钢", "综合钢筋", "HRB400")),
    (
        "商品混凝土",
        (
            "商品混凝土",
            "商品砼",
            "预拌混凝土",
            "C15混凝土",
            "C20混凝土",
            "C25混凝土",
            "C30混凝土",
            "C35混凝土",
            "商品砼 C25",
            "商品砼 C30",
        ),
    ),
    ("水泥", ("水泥", "水泥42.5", "水泥32.5", "普通硅酸盐水泥", "P.O42.5水泥")),
    ("砂", ("砂", "砂子", "中砂", "粗砂", "中粗砂", "河砂")),
    ("碎石", ("碎石", "石子", "级配碎石", "砾石")),
    ("生石灰", ("生石灰", "石灰")),
    ("标准砖", ("标准砖", "标砖", "烧结普通砖")),
    ("沥青", ("沥青", "道路石油沥青")),
    ("PE管", ("PE管", "PE管道", "HDPE管")),
)


def _norm_mat_unit(unit: str) -> str:
    u = unit.strip()
    return _UNIT_ALIASES.get(u.lower(), _UNIT_ALIASES.get(u, u))


def _canonical_mat_key(name: str) -> str:
    clean = re.sub(r"\s+", " ", name).strip()
    for canon, aliases in _MATERIAL_ALIAS_GROUPS:
        if clean in aliases:
            return canon
    if "商品砼" in clean or "商品混凝土" in clean or "预拌混凝土" in clean:
        return "商品混凝土"
    if "砂浆" in clean:
        return "砂浆"
    if "柴油" in clean:
        return "柴油"
    if "汽油" in clean:
        return "汽油"
    if "钢筋" in clean or "螺纹钢" in clean:
        return "钢筋"
    return clean


def _to_unit_price(rec: MaterialPriceRecord, target_unit: str) -> Optional[float]:
    val = rec.budget_price_excl_tax if rec.budget_price_excl_tax is not None else rec.base_price
    if val is None:
        return None
    src_u = _norm_mat_unit(rec.unit)
    dst_u = _norm_mat_unit(target_unit)
    if src_u == dst_u:
        return val
    if src_u == "t" and dst_u == "kg":
        return val / 1000.0
    if src_u == "kg" and dst_u == "t":
        return val * 1000.0
    return val


@PluginRegistry.register("adjusters")
class QuotaMaterialDiffAdjuster:
    """基于 L2 定额工料机消耗量的主材价差（价差表 + 常规主材 + 人工工日）动态重算算子"""

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
        handled_canon_keys: set[str] = set()
        price_diff_canon_keys = {
            _canonical_mat_key(l.name)
            for l in bd.resource_lines
            if l.resource_type == "价差" and l.quantity > 0 and l.name != "合计"
        }

        # Pass 1: 处理 L2 "价差" 行（如柴油、汽油、部分砂石砖价差条目）
        for line in bd.resource_lines:
            if line.resource_type == "价差" and line.quantity > 0 and line.name != "合计":
                target_mat = self._find_material(line.name, target_mat_map)
                if not target_mat:
                    continue
                new_mat_price = _to_unit_price(target_mat, line.unit)
                if new_mat_price is None:
                    continue

                hist_mat = self._find_material(line.name, hist_mat_map)
                old_mat_price = _to_unit_price(hist_mat, line.unit) if hist_mat else None
                base_price = hist_mat.base_price if (hist_mat and hist_mat.base_price is not None) else None
                if hist_mat and base_price is not None and _norm_mat_unit(hist_mat.unit) == "t" and _norm_mat_unit(line.unit) == "kg":
                    base_price = base_price / 1000.0

                if old_mat_price is not None:
                    diff_per_unit = new_mat_price - old_mat_price
                elif base_price is not None:
                    old_mat_price = base_price + line.unit_price
                    diff_per_unit = new_mat_price - old_mat_price
                else:
                    old_mat_price = line.unit_price
                    diff_per_unit = new_mat_price - old_mat_price

                handled_canon_keys.add(_canonical_mat_key(line.name))
                if abs(diff_per_unit) > 1e-4:
                    delta_line = line.quantity * diff_per_unit
                    delta_quota_yuan += delta_line
                    details.append(
                        f"主材[{line.name}]价差替换: {old_mat_price:.2f} -> {new_mat_price:.2f} 元/{line.unit} "
                        f"(消耗 {line.quantity}{line.unit}/{bd.quota_unit or ''}, 增减 {delta_line:+.2f}元)"
                    )

        # Pass 2: 处理常规 "材料" 行（钢筋、商砼、水泥、砂、碎石、PE管等，避开已走价差行的重叠材料）
        for line in bd.resource_lines:
            if (
                line.resource_type != "材料"
                or line.quantity <= 0
                or line.unit in ("%", "元")
                or line.name in ("合计", "其他材料费", "零星材料费")
            ):
                continue
            canon_key = _canonical_mat_key(line.name)
            if canon_key in handled_canon_keys or canon_key in price_diff_canon_keys:
                continue

            target_mat = self._find_material(line.name, target_mat_map)
            if not target_mat:
                continue
            new_mat_price = _to_unit_price(target_mat, line.unit)
            if new_mat_price is None:
                continue

            hist_mat = self._find_material(line.name, hist_mat_map)
            old_mat_price = _to_unit_price(hist_mat, line.unit) if hist_mat else None
            if old_mat_price is None or old_mat_price <= 0:
                old_mat_price = line.unit_price

            # 防异常量纲错配（如原始基价 <1 元/t 的定额异常基价）
            if old_mat_price <= 0 or (new_mat_price / old_mat_price > 10.0) or (new_mat_price / old_mat_price < 0.1):
                continue

            handled_canon_keys.add(canon_key)
            diff_per_unit = new_mat_price - old_mat_price
            if abs(diff_per_unit) > 1e-4:
                delta_line = line.quantity * diff_per_unit
                delta_quota_yuan += delta_line
                details.append(
                    f"主材[{line.name}]市场调差: {old_mat_price:.2f} -> {new_mat_price:.2f} 元/{line.unit} "
                    f"(消耗 {line.quantity}{line.unit}/{bd.quota_unit or ''}, 增减 {delta_line:+.2f}元)"
                )

        # Pass 3: 处理 "人工" 工日单价调差（叠加措施、间接费、利润与税金乘数）
        if target_ctx.labor_rates:
            ovh = (
                (1.0 + (bd.measure_rate_pct or 0.0) / 100.0)
                * (1.0 + (bd.indirect_rate_pct or 0.0) / 100.0)
                * (1.0 + (bd.profit_rate_pct or 0.0) / 100.0)
            )
            for line in bd.resource_lines:
                if line.resource_type != "人工" or line.unit != "工日" or line.quantity <= 0 or line.unit_price <= 0:
                    continue
                new_labor = target_ctx.labor_rates.get(line.name) or target_ctx.labor_rates.get("综合工日")
                if new_labor is None or abs(new_labor - line.unit_price) <= 1e-4:
                    continue
                delta_labor = line.quantity * (new_labor - line.unit_price) * ovh
                delta_quota_yuan += delta_labor
                details.append(
                    f"人工[{line.name}]工日调差: {line.unit_price:.2f} -> {new_labor:.2f} 元/工日 "
                    f"(消耗 {line.quantity}工日/{bd.quota_unit or ''}, 含取费增减 {delta_labor:+.2f}元)"
                )

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
        # 优先通过规范化同义词组匹配（防止 "砂" 误匹配 "水泥砂浆"）
        canon = _canonical_mat_key(name)
        for k, v in mat_map.items():
            if _canonical_mat_key(k) == canon:
                return v
        # 安全子串兜底：隔离砂浆/混凝土复合名称误命中
        blocked_pairs = (("砂", "砂浆"), ("砂", "砂砾"), ("水泥", "水泥砂浆"), ("水泥", "水泥混凝土"))
        for k, v in mat_map.items():
            if any((a in name and b in k) or (a in k and b in name) for a, b in blocked_pairs):
                continue
            if name in k or k in name:
                return v
        return None

    @staticmethod
    def _extract_quota_unit_factor(quota_unit: Optional[str], std_unit: Optional[str]) -> float:
        if not quota_unit:
            return 1.0
        m = re.match(r"(\d+)", quota_unit.strip())
        return float(m.group(1)) if m else 1.0
