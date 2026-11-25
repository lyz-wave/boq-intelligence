from pathlib import Path
import re
from typing import Optional

import xlrd

from src.core.contracts.boq_item import QuotaResourceLine, RawBoqItem, UnitPriceBreakdown
from src.core.contracts.project import CostStage, EngineeringDomain, PartialContext, ProjectFileBundle
from src.core.registry import PluginRegistry


@PluginRegistry.register("parsers")
class WaterEstimateXlsParser:
    """水利/生态工程单行本概算表 XLS 解析插件（支持万元自动转元及树状层级恢复）"""

    parser_id: str = "water_estimate_xls"
    priority: int = 20

    def can_handle(self, file_path: Path, header_preview: list[str]) -> bool:
        if file_path.suffix.lower() != ".xls":
            return False
        joined = " ".join(header_preview)
        return "工程或费用名称" in joined and ("建筑工程概算表" in joined or "合计（万元）" in joined)

    def parse(
        self, file_path: Path, bundle_ctx: ProjectFileBundle
    ) -> tuple[PartialContext, list[RawBoqItem]]:
        wb = xlrd.open_workbook(str(file_path))
        sheet = wb.sheet_by_index(0)
        is_wan_yuan = "万元" in str(sheet.cell_value(1, 5))

        items: list[RawBoqItem] = []
        # 栈元素: (depth, title)
        section_stack: list[tuple[int, str]] = []
        project_title: Optional[str] = None

        for r in range(2, sheet.nrows):
            seq = re.sub(r"\s+", "", str(sheet.cell_value(r, 0)))
            raw_name = self._clean_text(str(sheet.cell_value(r, 1)))
            unit = re.sub(r"\s+", "", str(sheet.cell_value(r, 2)))
            qty = self._to_float(sheet.cell_value(r, 3))
            unit_price = self._to_float(sheet.cell_value(r, 4))
            total_raw = self._to_float(sheet.cell_value(r, 5))

            if not raw_name:
                continue

            total_yuan = (
                round(total_raw * 10000.0, 2)
                if (total_raw is not None and is_wan_yuan)
                else total_raw
            )

            is_header = (not unit) and (qty is None) and (unit_price is None)
            if is_header:
                depth = 0 if not seq else len([p for p in seq.split(".") if p])
                if depth == 1 and not project_title:
                    project_title = raw_name
                while section_stack and section_stack[-1][0] >= depth:
                    section_stack.pop()
                section_stack.append((depth, raw_name))
                items.append(
                    RawBoqItem(
                        seq_no=seq,
                        section_path=[t for _, t in section_stack[:-1]],
                        is_section_header=True,
                        raw_name=raw_name,
                        total_price_yuan=total_yuan,
                        source_sheet=sheet.name,
                        source_row=r + 1,
                    )
                )
                continue

            if unit_price is None and qty and total_yuan is not None:
                unit_price = round(total_yuan / qty, 4)

            bd = (
                self._build_water_l2_breakdown(raw_name, unit or "项", unit_price)
                if (unit_price is not None and unit_price > 0 and unit)
                else None
            )

            items.append(
                RawBoqItem(
                    seq_no=seq,
                    section_path=[t for _, t in section_stack],
                    is_section_header=False,
                    raw_name=raw_name,
                    raw_features=raw_name,
                    raw_unit=unit or None,
                    quantity=qty,
                    unit_price_yuan=unit_price,
                    total_price_yuan=total_yuan,
                    breakdown=bd,
                    source_sheet=sheet.name,
                    source_row=r + 1,
                )
            )

        ctx = PartialContext(
            source_file=file_path.name,
            project_name=project_title or file_path.stem,
            domain=EngineeringDomain.WATER_ECOLOGY,
            stage=CostStage.PRELIMINARY_DESIGN,
        )
        return ctx, items

    @staticmethod
    def _build_water_l2_breakdown(name: str, unit: str, unit_price: float) -> UnitPriceBreakdown:
        if any(k in name for k in ("栽植", "黑沙蒿", "北沙柳", "柠条", "樟子松", "混播", "草地", "苦豆子", "养护")):
            quota_code = "SL-ST-101"
            mat_name, mat_unit = "生态修复苗木及草籽", unit
            labor_r, mat_r, mach_r = 0.28, 0.45, 0.08
        elif any(k in name for k in ("清表", "开挖", "回填", "清淤", "整平", "运输", "土方")):
            quota_code = "SL-TF-204"
            mat_name, mat_unit = "柴油", "kg"
            labor_r, mat_r, mach_r = 0.18, 0.14, 0.48
        else:
            quota_code = "SL-HY-308"
            mat_name, mat_unit = ("商品砼 C25", "m³") if ("砼" in name or "混凝土" in name) else ("格宾网箱与块石主材", unit)
            labor_r, mat_r, mach_r = 0.20, 0.52, 0.10

        labor_fee = round(unit_price * labor_r, 2)
        mat_fee = round(unit_price * mat_r, 2)
        mach_fee = round(unit_price * mach_r, 2)
        lines: list[QuotaResourceLine] = [
            QuotaResourceLine(
                resource_type="人工",
                name="水利中级工",
                unit="工日",
                quantity=round(labor_fee / 108.0, 4),
                unit_price=108.0,
                subtotal=labor_fee,
            )
        ]
        if mat_name == "柴油":
            qty_diesel = round(mat_fee / 7.4, 3)
            lines.append(
                QuotaResourceLine(
                    resource_type="价差",
                    name="柴油",
                    unit="kg",
                    quantity=qty_diesel,
                    unit_price=7.4,
                    subtotal=mat_fee,
                )
            )
        elif mat_name == "商品砼 C25":
            qty_c25 = round(mat_fee / 390.0, 3)
            lines.append(
                QuotaResourceLine(
                    resource_type="材料",
                    name="商品砼 C25",
                    unit="m³",
                    quantity=qty_c25,
                    unit_price=390.0,
                    subtotal=mat_fee,
                )
            )
        else:
            lines.append(
                QuotaResourceLine(
                    resource_type="材料",
                    name=mat_name,
                    unit=mat_unit,
                    quantity=1.0,
                    unit_price=mat_fee,
                    subtotal=mat_fee,
                )
            )
        if mach_fee > 0:
            lines.append(
                QuotaResourceLine(
                    resource_type="机械",
                    name="水利施工通用机械台班",
                    unit="台班",
                    quantity=round(mach_fee / 340.0, 4),
                    unit_price=340.0,
                    subtotal=mach_fee,
                )
            )
        return UnitPriceBreakdown(
            quota_codes=[quota_code],
            quota_unit=f"1{unit}",
            direct_labor_fee=labor_fee,
            direct_material_fee=mat_fee,
            direct_machinery_fee=mach_fee,
            measure_rate_pct=3.5,
            indirect_rate_pct=8.5,
            profit_rate_pct=5.0,
            tax_rate_pct=9.0,
            quota_total_yuan=round(unit_price, 2),
            resource_lines=lines,
        )

    @staticmethod
    def _clean_text(val: str) -> str:
        val = val.replace("\n", "")
        return re.sub(r"\s{2,}", " ", val).strip()

    @staticmethod
    def _to_float(val: object) -> Optional[float]:
        s = str(val).strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
