from pathlib import Path
import re
from typing import Any, Optional

import openpyxl
from openpyxl.utils import get_column_letter
import xlrd

from src.core.contracts.boq_item import RawBoqItem
from src.core.contracts.project import PartialContext, ProjectFileBundle
from src.core.registry import PluginRegistry


@PluginRegistry.register("parsers")
class TakeoffXlsxParser:
    """标准 .xlsx/.xlsm/.xls 清单与待组价提资表通用解析插件（支持多工作表筛选与自定义列映射）"""

    parser_id: str = "takeoff_xlsx"
    priority: int = 25

    _NON_BOQ_SHEET_KWS = ("封面", "扉页", "说明", "目录", "汇总表", "总表", "规费", "材料表", "主材")

    _HEADER_SYNONYMS: dict[str, tuple[str, ...]] = {
        "seq": ("序号", "编号", "细目号", "子目号", "项号"),
        "code": ("项目编码", "清单编码", "定额编号", "编码"),
        "name": ("项目名称", "工程或费用名称", "子目名称", "清单名称", "分部分项名称", "名称"),
        "features": ("项目特征", "特征描述", "规格型号", "技术特征", "特征", "规格", "技术要求"),
        "work_content": ("工作内容", "工程内容", "施工内容"),
        "unit": ("计量单位", "单位"),
        "qty": ("工程数量", "工程量", "暂估数量", "暂估工程量", "数量"),
        "unit_price": ("综合单价", "预算单价", "估算单价", "单价"),
        "total_price": ("合价", "合计", "总价", "金额"),
    }

    def can_handle(self, file_path: Path, header_preview: list[str]) -> bool:
        return file_path.suffix.lower() in {".xlsx", ".xlsm", ".xls"}

    def parse(
        self, file_path: Path, bundle_ctx: ProjectFileBundle
    ) -> tuple[PartialContext, list[RawBoqItem]]:
        items = self.parse_with_mapping(file_path, mapping_spec=None)
        ctx = PartialContext(
            source_file=file_path.name,
            project_name=bundle_ctx.project_name or file_path.stem,
            domain=bundle_ctx.domain_hint,
        )
        return ctx, items

    def inspect_workbook(self, file_path: Path, preview_rows: int = 12) -> dict[str, Any]:
        """探测工作簿全部 Sheet、表头行、推荐列映射及前若干行预览（用于前端交互式导入向导）."""
        sheets_data = self._read_workbook_sheets(file_path)
        inspected: list[dict[str, Any]] = []
        any_auto = False

        for sheet_name, grid in sheets_data:
            if not grid:
                inspected.append(
                    {
                        "sheet_name": sheet_name,
                        "row_count": 0,
                        "col_count": 0,
                        "header_row": 1,
                        "detected_columns": {},
                        "columns": [],
                        "preview": [],
                        "estimated_valid_rows": 0,
                        "auto_selected": False,
                    }
                )
                continue

            header_idx, col_map = self._locate_header(grid)
            col_count = max((len(r) for r in grid[: max(header_idx + 5, preview_rows)]), default=0)
            headers_row = grid[header_idx] if header_idx < len(grid) else []
            columns_meta = []
            for c_idx in range(col_count):
                h_txt = self._clean_cell(headers_row[c_idx]) if c_idx < len(headers_row) else ""
                if not h_txt and header_idx > 0 and c_idx < len(grid[header_idx - 1]):
                    h_txt = self._clean_cell(grid[header_idx - 1][c_idx])
                columns_meta.append(
                    {
                        "index": c_idx,
                        "letter": get_column_letter(c_idx + 1),
                        "header": h_txt or f"列 {get_column_letter(c_idx + 1)}",
                    }
                )

            parsed_preview_items = self._parse_grid_rows(sheet_name, grid, header_idx, col_map, prefix_sheet=False)
            valid_cnt = sum(1 for it in parsed_preview_items if not it.is_section_header)
            is_skip_sheet = any(sk in sheet_name for sk in self._NON_BOQ_SHEET_KWS)
            auto_sel = bool(valid_cnt > 0 and "name" in col_map and not is_skip_sheet)
            if auto_sel:
                any_auto = True

            preview = [
                [self._clean_cell(r[c]) if c < len(r) else "" for c in range(col_count)]
                for r in grid[:preview_rows]
            ]

            inspected.append(
                {
                    "sheet_name": sheet_name,
                    "row_count": len(grid),
                    "col_count": col_count,
                    "header_row": header_idx + 1,
                    "detected_columns": col_map,
                    "columns": columns_meta,
                    "preview": preview,
                    "estimated_valid_rows": valid_cnt,
                    "auto_selected": auto_sel,
                }
            )

        if not any_auto:
            for s in inspected:
                if s["row_count"] > 1:
                    s["auto_selected"] = True
                    break

        return {"sheets": inspected}

    def parse_with_mapping(
        self, file_path: Path, mapping_spec: Optional[dict[str, Any]] = None
    ) -> list[RawBoqItem]:
        """按指定的 Sheet 集合、表头行及列映射解析任意异构 Excel."""
        sheets_data = self._read_workbook_sheets(file_path)
        if not sheets_data:
            return []

        spec = mapping_spec or {}
        req_sheets = spec.get("sheets") or []
        global_header_row = spec.get("header_row")
        global_cols = self._sanitize_col_map(spec.get("columns"))
        sheet_overrides: dict[str, dict[str, Any]] = spec.get("sheet_overrides") or {}

        selected: list[tuple[str, list[list[Any]]]] = []
        if req_sheets:
            req_set = set(req_sheets)
            selected = [(n, g) for n, g in sheets_data if n in req_set]
        else:
            # 默认优先选择所有识别为有效清单页的非汇总/封面页
            for s_name, grid in sheets_data:
                if not grid:
                    continue
                if any(sk in s_name for sk in self._NON_BOQ_SHEET_KWS):
                    continue
                h_idx, cmap = self._locate_header(grid)
                if "name" in cmap:
                    parsed = self._parse_grid_rows(s_name, grid, h_idx, cmap, prefix_sheet=False)
                    if any(not it.is_section_header for it in parsed):
                        selected.append((s_name, grid))
            if not selected:
                selected = [sheets_data[0]]

        prefix_sheet = len(selected) > 1
        all_items: list[RawBoqItem] = []

        for s_name, grid in selected:
            if not grid:
                continue
            ov = sheet_overrides.get(s_name, {})
            det_header_idx, det_col_map = self._locate_header(grid)

            hdr_1based = ov.get("header_row") if ov.get("header_row") is not None else global_header_row
            header_idx = max(0, int(hdr_1based) - 1) if hdr_1based is not None else det_header_idx

            ov_cols = self._sanitize_col_map(ov.get("columns"))
            if ov_cols is not None:
                col_map = ov_cols
            elif global_cols is not None:
                col_map = global_cols
            else:
                col_map = det_col_map

            all_items.extend(
                self._parse_grid_rows(
                    sheet_name=s_name,
                    grid=grid,
                    header_idx=header_idx,
                    col_map=col_map,
                    prefix_sheet=prefix_sheet,
                )
            )
        return all_items

    def _read_workbook_sheets(self, file_path: Path) -> list[tuple[str, list[list[Any]]]]:
        suffix = file_path.suffix.lower()
        result: list[tuple[str, list[list[Any]]]] = []

        if suffix in {".xlsx", ".xlsm"}:
            wb = openpyxl.load_workbook(str(file_path), data_only=True)
            try:
                for sh in wb.worksheets:
                    max_r = min(sh.max_row or 0, 5000)
                    max_c = min(sh.max_column or 0, 30)
                    if max_r == 0 or max_c == 0:
                        result.append((sh.title, []))
                        continue
                    grid = [
                        [sh.cell(r, c).value for c in range(1, max_c + 1)]
                        for r in range(1, max_r + 1)
                    ]
                    # 展开前 18 行中的合并单元格表头值，兼容两级/三级合并表头
                    for rng in sh.merged_cells.ranges:
                        if rng.min_row > 18 or rng.min_col > max_c:
                            continue
                        top_val = grid[rng.min_row - 1][rng.min_col - 1]
                        if top_val is None:
                            continue
                        r_end = min(rng.max_row, 18, max_r)
                        c_end = min(rng.max_col, max_c)
                        for rr in range(rng.min_row - 1, r_end):
                            for cc in range(rng.min_col - 1, c_end):
                                if grid[rr][cc] is None:
                                    grid[rr][cc] = top_val
                    result.append((sh.title, grid))
            finally:
                wb.close()
        elif suffix == ".xls":
            wb_xls = xlrd.open_workbook(str(file_path), formatting_info=False)
            try:
                for sh in wb_xls.sheets():
                    max_r = min(sh.nrows, 5000)
                    max_c = min(sh.ncols, 30)
                    grid = [[sh.cell_value(r, c) for c in range(max_c)] for r in range(max_r)]
                    result.append((sh.name, grid))
            finally:
                wb_xls.release_resources()

        return result

    def _parse_grid_rows(
        self,
        sheet_name: str,
        grid: list[list[Any]],
        header_idx: int,
        col_map: dict[str, int],
        prefix_sheet: bool,
    ) -> list[RawBoqItem]:
        items: list[RawBoqItem] = []
        section_stack: list[tuple[int, str]] = []
        base_prefix = [sheet_name] if prefix_sheet else []

        for r in range(header_idx + 1, len(grid)):
            row_vals = grid[r]
            seq = self._cell_str(row_vals, col_map.get("seq"))
            code = self._cell_str(row_vals, col_map.get("code"))
            name = self._cell_str(row_vals, col_map.get("name"))
            feat = self._cell_str(row_vals, col_map.get("features"))
            work_c = self._cell_str(row_vals, col_map.get("work_content"))
            unit = self._cell_str(row_vals, col_map.get("unit"))
            qty = self._to_float(self._cell_val(row_vals, col_map.get("qty")))
            up = self._to_float(self._cell_val(row_vals, col_map.get("unit_price")))
            tot = self._to_float(self._cell_val(row_vals, col_map.get("total_price")))

            if not name and not feat:
                continue
            if name.replace(" ", "") in ("合计", "总计", "小计", "分部小计", "本页小计"):
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
                        section_path=[*base_prefix, *[t for _, t in section_stack[:-1]]],
                        is_section_header=True,
                        boq_code=code or None,
                        raw_name=name,
                        total_price_yuan=tot,
                        source_sheet=sheet_name,
                        source_row=r + 1,
                    )
                )
                continue

            items.append(
                RawBoqItem(
                    seq_no=seq,
                    section_path=[*base_prefix, *[t for _, t in section_stack]],
                    is_section_header=False,
                    boq_code=code or None,
                    raw_name=name or feat,
                    raw_features=feat or name,
                    raw_work_content=work_c,
                    raw_unit=unit or None,
                    quantity=qty,
                    unit_price_yuan=up,
                    total_price_yuan=tot,
                    source_sheet=sheet_name,
                    source_row=r + 1,
                )
            )
        return items

    def _locate_header(self, grid: list[list[Any]]) -> tuple[int, dict[str, int]]:
        scan_limit = min(15, len(grid))
        for r in range(scan_limit):
            row_strs = [self._clean_cell(v) for v in grid[r][:24]]
            joined = " ".join(row_strs)
            has_name_kw = any(k in joined for k in self._HEADER_SYNONYMS["name"])
            has_meta_kw = any(
                k in joined
                for k in (
                    *self._HEADER_SYNONYMS["unit"],
                    *self._HEADER_SYNONYMS["qty"],
                    *self._HEADER_SYNONYMS["features"],
                )
            )
            if has_name_kw and has_meta_kw:
                cmap = self._match_row_columns(row_strs)
                # 检查下一行是否为双层子表头（例如 合并行“工程量”下的“单位 | 数量”）
                if r + 1 < len(grid):
                    next_strs = [self._clean_cell(v) for v in grid[r + 1][:24]]
                    next_joined = " ".join(next_strs)
                    next_has_num = any(self._to_float(v) is not None for v in grid[r + 1][:24])
                    next_sub_hits = sum(
                        1 for k in ("单位", "数量", "单价", "合价", "金额", "特征") if k in next_joined
                    )
                    if next_sub_hits >= 1 and not next_has_num:
                        sub_cmap = self._match_row_columns(next_strs)
                        for k, v in sub_cmap.items():
                            cmap[k] = v
                        return r + 1, cmap
                return r, cmap
        return 0, {"seq": 0, "name": 1, "features": 2, "unit": 3, "qty": 4, "unit_price": 5, "total_price": 6}

    @classmethod
    def _match_row_columns(cls, row_strs: list[str]) -> dict[str, int]:
        cmap: dict[str, int] = {}
        priority_order = (
            "seq",
            "code",
            "name",
            "features",
            "work_content",
            "unit",
            "qty",
            "unit_price",
            "total_price",
        )
        for idx, raw_val in enumerate(row_strs):
            val = raw_val.replace(" ", "")
            if not val:
                continue
            matched_field: Optional[str] = None
            for field in priority_order:
                syns = cls._HEADER_SYNONYMS[field]
                if any(s in val for s in syns):
                    if field == "name" and "unit" in cmap and "特征" in val:
                        continue
                    matched_field = field
                    break
            if matched_field:
                cmap.setdefault(matched_field, idx)
        return cmap

    @staticmethod
    def _sanitize_col_map(raw_cols: Any) -> Optional[dict[str, int]]:
        if not isinstance(raw_cols, dict):
            return None
        cleaned: dict[str, int] = {}
        for k, v in raw_cols.items():
            if v is None or v == "" or v == -1:
                continue
            try:
                idx = int(v)
            except (TypeError, ValueError):
                continue
            if idx >= 0:
                cleaned[str(k)] = idx
        return cleaned

    @staticmethod
    def _cell_val(row_vals: list[Any], idx: Optional[int]) -> object:
        if idx is None or idx < 0 or idx >= len(row_vals):
            return None
        return row_vals[idx]

    @classmethod
    def _clean_cell(cls, v: Any) -> str:
        if v is None:
            return ""
        return re.sub(r"\s+", " ", str(v).replace("\n", " ")).strip()

    @classmethod
    def _cell_str(cls, row_vals: list[Any], idx: Optional[int]) -> str:
        return cls._clean_cell(cls._cell_val(row_vals, idx))

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
