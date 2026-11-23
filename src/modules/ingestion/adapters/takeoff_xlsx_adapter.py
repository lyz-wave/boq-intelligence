from pathlib import Path
import re
from typing import Optional

import openpyxl

from src.core.contracts.boq_item import RawBoqItem
from src.core.contracts.project import PartialContext, ProjectFileBundle
from src.core.registry import PluginRegistry


@PluginRegistry.register("parsers")
class TakeoffXlsxParser:
    """标准 .xlsx 清单与待组价提资表通用解析插件"""

    parser_id: str = "takeoff_xlsx"
    priority: int = 25

    def can_handle(self, file_path: Path, header_preview: list[str]) -> bool:
        return file_path.suffix.lower() in {".xlsx", ".xlsm"}

    def parse(
        self, file_path: Path, bundle_ctx: ProjectFileBundle
    ) -> tuple[PartialContext, list[RawBoqItem]]:
        wb = openpyxl.load_workbook(str(file_path), data_only=True)
        sh = wb.active

        header_row_idx, col_map = self._locate_header(sh)
        items: list[RawBoqItem] = []
        section_stack: list[tuple[int, str]] = []

        for r in range(header_row_idx + 1, sh.max_row + 1):
            row_vals = [sh.cell(r, c).value for c in range(1, sh.max_column + 1)]
            seq = self._cell_str(row_vals, col_map.get("seq"))
            code = self._cell_str(row_vals, col_map.get("code"))
            name = self._cell_str(row_vals, col_map.get("name"))
            feat = self._cell_str(row_vals, col_map.get("features"))
            unit = self._cell_str(row_vals, col_map.get("unit"))
            qty = self._to_float(self._cell_val(row_vals, col_map.get("qty")))
            up = self._to_float(self._cell_val(row_vals, col_map.get("unit_price")))
            tot = self._to_float(self._cell_val(row_vals, col_map.get("total_price")))

            if not name and not feat:
                continue
            if name in ("合计", "合 计", "总计"):
                continue

            is_header = (not unit) and (qty is None) and (up is None)
            if is_header:
                depth = len([p for p in seq.split(".") if p]) if seq else 1
                while section_stack and section_stack[-1][0] >= depth:
                    section_stack.pop()
                section_stack.append((depth, name))
                items.append(
                    RawBoqItem(
                        seq_no=seq,
                        section_path=[t for _, t in section_stack[:-1]],
                        is_section_header=True,
                        boq_code=code or None,
                        raw_name=name,
                        total_price_yuan=tot,
                        source_sheet=sh.title,
                        source_row=r,
                    )
                )
                continue

            items.append(
                RawBoqItem(
                    seq_no=seq,
                    section_path=[t for _, t in section_stack],
                    is_section_header=False,
                    boq_code=code or None,
                    raw_name=name or feat,
                    raw_features=feat or name,
                    raw_unit=unit or None,
                    quantity=qty,
                    unit_price_yuan=up,
                    total_price_yuan=tot,
                    source_sheet=sh.title,
                    source_row=r,
                )
            )

        ctx = PartialContext(
            source_file=file_path.name,
            project_name=bundle_ctx.project_name or file_path.stem,
            domain=bundle_ctx.domain_hint,
        )
        return ctx, items

    def _locate_header(self, sh: openpyxl.worksheet.worksheet.Worksheet) -> tuple[int, dict[str, int]]:
        for r in range(1, min(15, sh.max_row + 1)):
            row_strs = [str(sh.cell(r, c).value or "").strip() for c in range(1, min(20, sh.max_column + 1))]
            joined = " ".join(row_strs)
            if any(k in joined for k in ("项目名称", "工程或费用名称", "名称")) and any(
                k in joined for k in ("单位", "工程数量", "数量", "项目特征")
            ):
                cmap: dict[str, int] = {}
                for idx, val in enumerate(row_strs):
                    if "序号" in val or "编号" in val:
                        cmap.setdefault("seq", idx)
                    elif "项目编码" in val or "清单编码" in val:
                        cmap.setdefault("code", idx)
                    elif "项目名称" in val or "费用名称" in val or "名称" == val:
                        cmap.setdefault("name", idx)
                    elif "特征" in val or "规格" in val:
                        cmap.setdefault("features", idx)
                    elif "单位" in val:
                        cmap.setdefault("unit", idx)
                    elif "数量" in val:
                        cmap.setdefault("qty", idx)
                    elif "单价" in val:
                        cmap.setdefault("unit_price", idx)
                    elif "合价" in val or "合计" in val or "金额" in val:
                        cmap.setdefault("total_price", idx)
                return r, cmap
        return 1, {"seq": 0, "name": 1, "features": 2, "unit": 3, "qty": 4, "unit_price": 5, "total_price": 6}

    @staticmethod
    def _cell_val(row_vals: list, idx: Optional[int]) -> object:
        if idx is None or idx >= len(row_vals):
            return None
        return row_vals[idx]

    @classmethod
    def _cell_str(cls, row_vals: list, idx: Optional[int]) -> str:
        v = cls._cell_val(row_vals, idx)
        if v is None:
            return ""
        return re.sub(r"\s+", " ", str(v).replace("\n", " ")).strip()

    @staticmethod
    def _to_float(val: object) -> Optional[float]:
        if val is None or isinstance(val, bool):
            return None
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val).strip()
        if not s or s.startswith("="):
            return None
        try:
            return float(s.replace(",", ""))
        except ValueError:
            return None
