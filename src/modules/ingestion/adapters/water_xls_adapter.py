from pathlib import Path
import re
from typing import Optional

import xlrd

from src.core.contracts.boq_item import RawBoqItem
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
