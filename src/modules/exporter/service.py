from io import BytesIO
from typing import Any, Optional

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from src.core.contracts.boq_item import CanonicalProject
from src.core.contracts.matching import AuditFinding, ExportOptions


class ExporterModule:
    """标准造价报表生成深模块：对外仅暴露 export_estimate_excel(project, options) -> bytes."""

    _HDR_FILL = PatternFill("solid", fgColor="1E3A5F")
    _HDR_FONT = Font(name="Microsoft YaHei", size=10, bold=True, color="FFFFFF")
    _SEC_FILL = PatternFill("solid", fgColor="EAF1F8")
    _SEC_FONT = Font(name="Microsoft YaHei", size=10, bold=True, color="1E3A5F")
    _BODY_FONT = Font(name="Microsoft YaHei", size=10, color="1F2937")
    _WARN_FILL = PatternFill("solid", fgColor="FEF3C7")
    _ERR_FILL = PatternFill("solid", fgColor="FEE2E2")
    _THIN = Side(border_style="thin", color="CBD5E1")
    _BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)

    def export_estimate_excel(
        self,
        project: CanonicalProject,
        options: Optional[ExportOptions] = None,
        match_details: Optional[dict[str, dict[str, Any]]] = None,
    ) -> bytes:
        opts = options or ExportOptions()
        details_map = match_details or {}
        wb = openpyxl.Workbook()

        # Sheet 1: 工程量清单估算概算表
        ws = wb.active
        ws.title = "工程量清单估算表"
        self._build_estimate_sheet(ws, project, opts, details_map)

        # Sheet 2: 智能匹配与调价溯源表
        if opts.include_provenance_sheet:
            ws_prov = wb.create_sheet("智能匹配与定额溯源表")
            self._build_provenance_sheet(ws_prov, project, details_map)

        # Sheet 3: BQ合规与合理性审查报告
        if opts.include_audit_sheet:
            ws_audit = wb.create_sheet("BQ合规与合理性审查报告")
            self._build_audit_sheet(ws_audit, project.audit_findings)

        buf = BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def export_takeoff_template(self, domain_name: str = "工程量清单标准提资模板") -> bytes:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "标准提资清单"
        headers = ["序号", "项目编码", "项目名称", "项目特征描述", "计量单位", "工程数量"]
        ws.append([domain_name, "", "", "", "", ""])
        ws.merge_cells("A1:F1")
        ws["A1"].font = Font(name="Microsoft YaHei", size=13, bold=True, color="1E3A5F")
        ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 28

        ws.append(headers)
        for col_idx in range(1, 7):
            c = ws.cell(2, col_idx)
            c.fill = self._HDR_FILL
            c.font = self._HDR_FONT
            c.alignment = Alignment(horizontal="center", vertical="center")

        sample_rows = [
            ["1", "", "灌木与植被恢复", "", "", ""],
            ["1.1", "", "黑沙蒿栽植", "规格：地径≥0.3cm，高度25~30cm，种植形式：1m×3m，抚育：3年", "hm²", 5.2],
            ["1.2", "", "土（石）方运输", "运距5km", "m³", 1200.0],
            ["1.3", "030409001009", "电力电缆", "型号:ZR-YJV-0.6/1KV-3×150+1×95，敷设方式:室内", "m", 120.0],
        ]
        for row in sample_rows:
            ws.append(row)

        widths = [10, 18, 24, 48, 12, 14]
        for idx, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(idx)].width = w

        buf = BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def _build_estimate_sheet(
        self,
        ws: openpyxl.worksheet.worksheet.Worksheet,
        project: CanonicalProject,
        opts: ExportOptions,
        details_map: dict[str, dict[str, Any]],
    ) -> None:
        ctx = project.context
        tot_label = f"合价（{opts.currency_unit}）"
        ws.append([f"{ctx.project_name} — {ctx.stage.value}智能组价表"])
        ws.merge_cells("A1:J1")
        ws["A1"].font = Font(name="Microsoft YaHei", size=14, bold=True, color="1E3A5F")
        ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 30

        meta_line = (
            f"地区: {ctx.province}{ctx.city or ''}  |  专业领域: {ctx.domain.value}  |  "
            f"价格基准期: {ctx.price_base_month}  |  主要依据: {', '.join(ctx.quota_standards[:2]) or '参考历史相似工程'}"
        )
        ws.append([meta_line])
        ws.merge_cells("A2:J2")
        ws["A2"].font = Font(name="Microsoft YaHei", size=9, color="475569")
        ws["A2"].alignment = Alignment(horizontal="center", vertical="center")

        headers = [
            "序号",
            "项目编码",
            "工程或清单名称",
            "标准化特征摘要",
            "单位",
            "工程数量",
            "综合单价（元）",
            tot_label,
            "置信度",
            "价格溯源与定额依据",
        ]
        ws.append(headers)
        for col_idx in range(1, 11):
            cell = ws.cell(3, col_idx)
            cell.fill = self._HDR_FILL
            cell.font = self._HDR_FONT
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = self._BORDER
        ws.row_dimensions[3].height = 24

        row_cursor = 4
        emitted_sections: set[tuple[str, ...]] = set()
        leaf_rows: list[int] = []

        for item in project.items:
            # 逐层输出分部标题行
            for depth in range(1, len(item.section_path) + 1):
                prefix = tuple(item.section_path[:depth])
                if prefix not in emitted_sections:
                    emitted_sections.add(prefix)
                    ws.cell(row_cursor, 1, f"S{depth}")
                    ws.cell(row_cursor, 3, prefix[-1])
                    for c_i in range(1, 11):
                        c = ws.cell(row_cursor, c_i)
                        c.fill = self._SEC_FILL
                        c.font = self._SEC_FONT
                        c.border = self._BORDER
                    row_cursor += 1

            det = details_map.get(item.item_id, {})
            conf = det.get("confidence", "high")
            conf_label = {"high": "高(绿)", "medium": "中(黄)", "low": "低(红)"}.get(conf, conf)
            provenance = det.get("provenance_summary", "历史库直接入库")
            slot_summary = "; ".join(
                f"{sv.slot_label}:{sv.normalized_value}" for sv in item.slots.values()
            ) or item.raw_text_full[:60]

            qty = item.normalized_quantity or 0.0
            up = item.normalized_unit_price_yuan or 0.0
            div = 10000.0 if opts.currency_unit == "万元" else 1.0

            ws.cell(row_cursor, 1, item.seq_no or str(len(leaf_rows) + 1))
            ws.cell(row_cursor, 2, item.boq_code or "")
            ws.cell(row_cursor, 3, item.standard_name)
            ws.cell(row_cursor, 4, slot_summary)
            ws.cell(row_cursor, 5, item.standard_unit or "")
            ws.cell(row_cursor, 6, qty)
            ws.cell(row_cursor, 7, round(up, 2))
            if opts.include_formula:
                formula = f"=ROUND(F{row_cursor}*G{row_cursor}/{div},2)" if div > 1 else f"=ROUND(F{row_cursor}*G{row_cursor},2)"
                ws.cell(row_cursor, 8, formula)
            else:
                ws.cell(row_cursor, 8, round((qty * up) / div, 2))
            ws.cell(row_cursor, 9, conf_label)
            ws.cell(row_cursor, 10, provenance)

            for c_i in range(1, 11):
                c = ws.cell(row_cursor, c_i)
                c.font = self._BODY_FONT
                c.border = self._BORDER
                if c_i in (6, 7, 8):
                    c.number_format = "#,##0.00"
                    c.alignment = Alignment(horizontal="right", vertical="center")
                elif c_i in (1, 5, 9):
                    c.alignment = Alignment(horizontal="center", vertical="center")
                else:
                    c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)

            if conf == "low":
                ws.cell(row_cursor, 9).fill = self._ERR_FILL
            elif conf == "medium":
                ws.cell(row_cursor, 9).fill = self._WARN_FILL

            leaf_rows.append(row_cursor)
            row_cursor += 1

        # 汇总合计行
        ws.cell(row_cursor, 3, f"分部分项工程合价总计（{opts.currency_unit}）")
        if opts.include_formula and leaf_rows:
            sum_expr = f"=SUM(H{leaf_rows[0]}:H{leaf_rows[-1]})"
            # 仅累加明细行避免潜在区间空值影响，使用 SUMIF 或直接范围（分部行 H 列为空）
            ws.cell(row_cursor, 8, sum_expr)
        else:
            ws.cell(row_cursor, 8, round(sum((it.normalized_quantity or 0) * (it.normalized_unit_price_yuan or 0) for it in project.items) / (10000.0 if opts.currency_unit == "万元" else 1.0), 2))

        for c_i in range(1, 11):
            c = ws.cell(row_cursor, c_i)
            c.fill = self._SEC_FILL
            c.font = self._SEC_FONT
            c.border = self._BORDER
            if c_i == 8:
                c.number_format = "#,##0.00"

        widths = [10, 16, 24, 38, 9, 13, 15, 16, 11, 38]
        for idx, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(idx)].width = w

    def _build_provenance_sheet(
        self,
        ws: openpyxl.worksheet.worksheet.Worksheet,
        project: CanonicalProject,
        details_map: dict[str, dict[str, Any]],
    ) -> None:
        headers = ["序号", "清单名称", "匹配相似度", "硬规格核分", "调价方式", "原历史单价", "调价后单价", "特征对比与调差明细"]
        ws.append(headers)
        for c_i in range(1, 9):
            c = ws.cell(1, c_i)
            c.fill = self._HDR_FILL
            c.font = self._HDR_FONT
            c.border = self._BORDER
        for it in project.items:
            det = details_map.get(it.item_id, {})
            ws.append(
                [
                    it.seq_no,
                    it.standard_name,
                    det.get("final_score", "-"),
                    det.get("hard_spec_kernel_score", "-"),
                    det.get("adjuster_used", "-"),
                    det.get("original_unit_price", it.normalized_unit_price_yuan or 0.0),
                    it.normalized_unit_price_yuan or 0.0,
                    det.get("diff_summary", det.get("provenance_summary", "")),
                ]
            )
        for idx, w in enumerate([10, 24, 12, 12, 18, 14, 14, 52], start=1):
            ws.column_dimensions[get_column_letter(idx)].width = w

    def _build_audit_sheet(
        self, ws: openpyxl.worksheet.worksheet.Worksheet, findings: list[Any]
    ) -> None:
        headers = ["规则编号", "规则名称", "严重等级", "清单序号", "审查问题描述", "修改建议"]
        ws.append(headers)
        for c_i in range(1, 7):
            c = ws.cell(1, c_i)
            c.fill = self._HDR_FILL
            c.font = self._HDR_FONT
            c.border = self._BORDER
        if not findings:
            ws.append(["ALL_PASS", "全部规则通过", "notice", "-", "未发现格式、算术或偏离异常", "可提交三级校审"])
        for f in findings:
            fd = f if isinstance(f, AuditFinding) else AuditFinding(**f)
            ws.append([fd.rule_id, fd.rule_name, fd.severity.value, fd.seq_no or "-", fd.message, fd.suggestion or ""])
        for idx, w in enumerate([12, 24, 12, 12, 48, 38], start=1):
            ws.column_dimensions[get_column_letter(idx)].width = w
