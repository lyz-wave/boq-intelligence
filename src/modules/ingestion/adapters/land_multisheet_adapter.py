from pathlib import Path
import re
from typing import Optional

import xlrd

from src.core.contracts.boq_item import QuotaResourceLine, RawBoqItem, UnitPriceBreakdown
from src.core.contracts.project import (
    CostStage,
    EngineeringDomain,
    MaterialPriceRecord,
    PartialContext,
    ProjectFileBundle,
)
from src.core.registry import PluginRegistry


@PluginRegistry.register("parsers")
class LandMultiSheetXlsParser:
    """土地综合整治多 Sheet 联动解析插件（报价表 + 单价汇总表 + 清单单价分析表 + 主次材汇总）"""

    parser_id: str = "land_multisheet_xls"
    priority: int = 15

    def can_handle(self, file_path: Path, header_preview: list[str]) -> bool:
        if file_path.suffix.lower() != ".xls":
            return False
        wb = xlrd.open_workbook(str(file_path), on_demand=True)
        names = " ".join(wb.sheet_names())
        wb.release_resources()
        return "工程量清单报价表" in names and any("单价分析表" in s for s in names.split())

    def parse(
        self, file_path: Path, bundle_ctx: ProjectFileBundle
    ) -> tuple[PartialContext, list[RawBoqItem]]:
        wb = xlrd.open_workbook(str(file_path))
        breakdowns_by_code, breakdowns_by_name = self._parse_analysis_sheets(wb)
        summary_fees = self._parse_summary_sheet(wb)
        materials = self._parse_material_sheets(wb)

        quote_sheet_name = next(s for s in wb.sheet_names() if "工程量清单报价表" in s)
        sh = wb.sheet_by_name(quote_sheet_name)

        raw_rows: list[tuple[int, str, str, str, Optional[float], Optional[float], Optional[float]]] = []
        for r in range(3, sh.nrows):
            seq = re.sub(r"\s+", "", str(sh.cell_value(r, 0)))
            name = self._clean_text(str(sh.cell_value(r, 2)))
            unit = re.sub(r"\s+", "", str(sh.cell_value(r, 5)))
            qty = self._to_float(sh.cell_value(r, 7))
            up = self._to_float(sh.cell_value(r, 9))
            tot = self._to_float(sh.cell_value(r, 11))
            if not name or name in ("工程项目及名称", "合计", "合  计") or seq == "编号":
                continue
            raw_rows.append((r + 1, seq, name, unit, qty, up, tot))

        items: list[RawBoqItem] = []
        section_stack: list[tuple[int, str]] = []
        last_seq_with_children = ""

        for idx, (row_no, seq, name, unit, qty, up, tot) in enumerate(raw_rows):
            next_seq = raw_rows[idx + 1][1] if idx + 1 < len(raw_rows) else ""
            is_parent = (not unit) or (bool(seq) and next_seq.startswith(seq + "."))

            if is_parent:
                depth = len([p for p in seq.split(".") if p]) if seq else 99
                while section_stack and section_stack[-1][0] >= depth:
                    section_stack.pop()
                section_stack.append((depth, name))
                last_seq_with_children = seq
                items.append(
                    RawBoqItem(
                        seq_no=seq,
                        section_path=[t for _, t in section_stack[:-1]],
                        is_section_header=True,
                        raw_name=name,
                        raw_unit=unit or None,
                        quantity=qty,
                        unit_price_yuan=up,
                        total_price_yuan=tot,
                        source_sheet=quote_sheet_name,
                        source_row=row_no,
                    )
                )
                continue

            # 若遇到空序号的子件（如 Dn110三通），归属到最近一次父级章节路径
            if not seq and last_seq_with_children and section_stack:
                cur_path = [t for _, t in section_stack]
            else:
                cur_depth = len([p for p in seq.split(".") if p]) if seq else 99
                cur_path = [t for d, t in section_stack if d < cur_depth]

            breakdown = breakdowns_by_code.get(seq) or breakdowns_by_name.get(name)
            if breakdown is None and seq in summary_fees:
                labor, mat, mach = summary_fees[seq]
                breakdown = UnitPriceBreakdown(
                    direct_labor_fee=labor,
                    direct_material_fee=mat,
                    direct_machinery_fee=mach,
                )

            items.append(
                RawBoqItem(
                    seq_no=seq,
                    section_path=cur_path,
                    is_section_header=False,
                    raw_name=name,
                    raw_features=name,
                    raw_unit=unit or None,
                    quantity=qty,
                    unit_price_yuan=up,
                    total_price_yuan=tot,
                    breakdown=breakdown,
                    source_sheet=quote_sheet_name,
                    source_row=row_no,
                )
            )

        ctx = PartialContext(
            source_file=file_path.name,
            project_name=file_path.stem,
            domain=EngineeringDomain.LAND_CONSOLIDATION,
            stage=CostStage.CONSTRUCTION_DRAWING,
            base_materials=materials,
        )
        return ctx, items

    def _parse_summary_sheet(self, wb: xlrd.book.Book) -> dict[str, tuple[float, float, float]]:
        result: dict[str, tuple[float, float, float]] = {}
        name = next((s for s in wb.sheet_names() if "单价汇总表" in s), None)
        if not name:
            return result
        sh = wb.sheet_by_name(name)
        for r in range(3, sh.nrows):
            seq = re.sub(r"\s+", "", str(sh.cell_value(r, 0)))
            if not seq or seq == "编号":
                continue
            labor = self._to_float(sh.cell_value(r, 5)) or 0.0
            mat = self._to_float(sh.cell_value(r, 7)) or 0.0
            mach = self._to_float(sh.cell_value(r, 9)) or 0.0
            result[seq] = (labor, mat, mach)
        return result

    def _parse_analysis_sheets(
        self, wb: xlrd.book.Book
    ) -> tuple[dict[str, UnitPriceBreakdown], dict[str, UnitPriceBreakdown]]:
        by_code: dict[str, UnitPriceBreakdown] = {}
        by_name: dict[str, UnitPriceBreakdown] = {}
        analysis_sheets = [s for s in wb.sheet_names() if "单价分析表" in s]

        for sname in analysis_sheets:
            sh = wb.sheet_by_name(sname)
            blocks: list[dict] = []
            cur: Optional[dict] = None
            for r in range(sh.nrows):
                c0 = str(sh.cell_value(r, 0)).strip()
                c2 = self._clean_text(str(sh.cell_value(r, 2)))
                c7 = re.sub(r"\s+", "", str(sh.cell_value(r, 7)))
                c9 = self._to_float(sh.cell_value(r, 9))
                c11 = self._to_float(sh.cell_value(r, 11))
                c13_raw = self._clean_text(str(sh.cell_value(r, 13)))
                c13_num = self._to_float(sh.cell_value(r, 13))

                if c0 == "单价名称:":
                    if cur:
                        blocks.append(cur)
                    cur = {"name": c2, "codes_raw": c13_raw, "quota": "", "unit": "", "lines": []}
                elif c0 == "定额依据:" and cur:
                    cur["quota"] = c2
                    cur["unit"] = c13_raw
                elif cur and c2:
                    cur["lines"].append((c0, c2, c7, c9, c11, c13_num))
            if cur:
                blocks.append(cur)

            for blk in blocks:
                bd = self._build_breakdown(blk)
                if blk["name"]:
                    by_name[blk["name"]] = bd
                for code in re.split(r"[/、,，\s]+", blk["codes_raw"]):
                    c = code.strip()
                    if c:
                        by_code[c] = bd
        return by_code, by_name

    def _build_breakdown(self, blk: dict) -> UnitPriceBreakdown:
        quota_codes = [q.strip() for q in re.split(r"[＋+、,，\s]+", blk["quota"]) if q.strip()]
        bd = UnitPriceBreakdown(quota_codes=quota_codes, quota_unit=blk["unit"] or None)
        cur_section = "人工"

        for c0, name, unit, qty, up, sub in blk["lines"]:
            if name in ("直接费", "直接工程费", "编号"):
                continue
            if c0 == "1" and name == "人工费":
                cur_section = "人工"
                bd.direct_labor_fee = sub
                continue
            if c0 == "2" and name == "材料费":
                cur_section = "材料"
                bd.direct_material_fee = sub
                continue
            if c0 == "3" and name == "机械费":
                cur_section = "机械"
                bd.direct_machinery_fee = sub
                continue
            if c0 == "(二)" and name == "措施费":
                bd.measure_rate_pct = qty
                bd.measure_fee = sub
                continue
            if c0 == "二" and name == "间接费":
                bd.indirect_rate_pct = qty
                bd.indirect_fee = sub
                continue
            if c0 == "三" and name == "利润":
                bd.profit_rate_pct = qty
                bd.profit_fee = sub
                continue
            if c0 == "四" and name == "价差":
                cur_section = "价差"
                bd.price_diff_fee = sub
                continue
            if c0 == "五" and name == "税金":
                bd.tax_rate_pct = qty
                bd.tax_fee = sub
                continue
            if c0 == "合计":
                bd.quota_total_yuan = sub
                continue

            if qty is not None or sub is not None:
                bd.resource_lines.append(
                    QuotaResourceLine(
                        resource_type=cur_section,
                        name=name,
                        unit=unit,
                        quantity=qty or 0.0,
                        unit_price=up or 0.0,
                        subtotal=sub or 0.0,
                    )
                )
        return bd

    def _parse_material_sheets(self, wb: xlrd.book.Book) -> list[MaterialPriceRecord]:
        materials: list[MaterialPriceRecord] = []
        for sname in wb.sheet_names():
            if "材料用量汇总表" not in sname:
                continue
            sh = wb.sheet_by_name(sname)
            for r in range(2, sh.nrows):
                name = self._clean_text(str(sh.cell_value(r, 1)))
                unit = re.sub(r"\s+", "", str(sh.cell_value(r, 6)))
                up = self._to_float(sh.cell_value(r, 8))
                if name and up is not None:
                    materials.append(
                        MaterialPriceRecord(
                            material_name=name,
                            unit=unit,
                            budget_price_excl_tax=up,
                        )
                    )
        return materials

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
