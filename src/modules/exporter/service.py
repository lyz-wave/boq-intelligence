from datetime import date
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
    _TOT_FILL = PatternFill("solid", fgColor="DCE8F5")
    _TOT_FONT = Font(name="Microsoft YaHei", size=11, bold=True, color="0F2942")
    _BODY_FONT = Font(name="Microsoft YaHei", size=10, color="1F2937")
    _MUTED_FONT = Font(name="Microsoft YaHei", size=9.5, italic=True, color="475569")
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

        if opts.include_summary_sheet:
            ws_summary = wb.active
            ws_summary.title = "工程总投资估算汇总表"
            ws_est = wb.create_sheet("工程量清单估算表")
        else:
            ws_summary = None
            ws_est = wb.active
            ws_est.title = "工程量清单估算表"

        div_total_row, div_amount_raw = self._build_estimate_sheet(ws_est, project, opts, details_map)

        measure_total_row: Optional[int] = None
        if opts.include_measure_sheet:
            ws_meas = wb.create_sheet("措施项目与取费表")
            measure_total_row = self._build_measure_sheet(
                ws_meas, project, opts, div_total_row=div_total_row, div_amount_raw=div_amount_raw
            )

        if ws_summary is not None:
            self._build_summary_sheet(
                ws_summary,
                project,
                opts,
                div_total_row=div_total_row,
                measure_total_row=measure_total_row,
                div_amount_raw=div_amount_raw,
            )

        if opts.include_provenance_sheet:
            ws_prov = wb.create_sheet("智能匹配与定额溯源表")
            self._build_provenance_sheet(ws_prov, project, details_map)

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
            ["1.1", "", "黑沙蒿栽植", "规格：地径≥0.3cm，高度25~30cm，种植形式：1m×3m标准：220株/亩，抚育：3年", "hm²", 5.2],
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

    def _build_summary_sheet(
        self,
        ws: openpyxl.worksheet.worksheet.Worksheet,
        project: CanonicalProject,
        opts: ExportOptions,
        div_total_row: int,
        measure_total_row: Optional[int],
        div_amount_raw: float,
    ) -> None:
        ctx = project.context
        u_lbl = opts.currency_unit
        ws.append([f"{ctx.project_name} — 工程总投资估算汇总表"])
        ws.merge_cells("A1:F1")
        ws["A1"].font = Font(name="Microsoft YaHei", size=14, bold=True, color="1E3A5F")
        ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 30

        meta = (
            f"建设地点: {ctx.province}{ctx.city or ''}  |  专业领域: {ctx.domain.value}  |  "
            f"计价阶段: {ctx.stage.value}  |  价格基准期: {ctx.price_base_month}  |  货币口径: {u_lbl}"
        )
        ws.append([meta])
        ws.merge_cells("A2:F2")
        ws["A2"].font = Font(name="Microsoft YaHei", size=9, color="475569")
        ws["A2"].alignment = Alignment(horizontal="center", vertical="center")

        headers = ["序号", "工程或费用项目名称", "计算基数与费率依据", f"金额（{u_lbl}）", "占总投资比例", "备注"]
        ws.append(headers)
        for c_i in range(1, 7):
            c = ws.cell(3, c_i)
            c.fill = self._HDR_FILL
            c.font = self._HDR_FONT
            c.alignment = Alignment(horizontal="center", vertical="center")
            c.border = self._BORDER
        ws.row_dimensions[3].height = 24

        m_rate = max(0.0, opts.measure_rate_pct) / 100.0
        o_rate = max(0.0, opts.other_project_rate_pct) / 100.0
        c_rate = max(0.0, opts.contingency_rate_pct) / 100.0
        vat_rate = max(0.0, opts.vat_rate_pct) / 100.0

        meas_raw = round(div_amount_raw * m_rate, 2)
        part1_raw = round(div_amount_raw + meas_raw, 2)
        excl_tax_raw = round(part1_raw / (1.0 + vat_rate), 2)
        tax_raw = round(part1_raw - excl_tax_raw, 2)
        other_raw = round(part1_raw * o_rate, 2)
        part12_raw = round(part1_raw + other_raw, 2)
        cont_raw = round(part12_raw * c_rate, 2)
        grand_raw = round(part12_raw + cont_raw, 2) or 1.0

        div_ref = f"='工程量清单估算表'!H{div_total_row}"
        meas_ref = (
            f"='措施项目与取费表'!F{measure_total_row}"
            if measure_total_row
            else f"=ROUND(D5*{m_rate:.4f},2)"
        )

        # Row 4..16 清晰结构，合计不重复计入“其中：除税/税额”披露行
        # Row 4: 一、第一部分 工程费用（建筑安装工程费） = D5 + D6
        # Row 5:   1.1 分部分项工程费
        # Row 6:   1.2 措施项目费
        # Row 7:     其中：除税建筑安装工程费 (披露项)
        # Row 8:     其中：增值税销项税额 (披露项)
        # Row 9: 二、第二部分 工程建设其他费用 = SUM(D10:D13)
        # Row 10..13: 管理费/勘察设计/监理/专项咨询
        # Row 14: 一至二部分投资合计 = D4 + D9
        # Row 15: 三、第三部分 基本预备费 = ROUND(D14 * c_rate, 2)
        # Row 16: 工程静态总投资总计 = D14 + D15
        grand_row = 16
        rows_spec = [
            ("一", "第一部分 工程费用（建筑安装工程费）", "分部分项工程费 + 措施项目费", "=D5+D6" if opts.include_formula else part1_raw, "section", part1_raw / grand_raw, "第一部分合计"),
            ("1.1", "分部分项工程费", "各分部清单智能组价汇总结算", div_ref if opts.include_formula else div_amount_raw, "body", div_amount_raw / grand_raw, "见表《工程量清单估算表》"),
            ("1.2", "措施项目费", f"分部分项工程费 × {opts.measure_rate_pct:.2f}%", meas_ref if opts.include_formula else meas_raw, "body", meas_raw / grand_raw, "见表《措施项目与取费表》"),
            ("-", "  其中：除税建筑安装工程费", f"含税建安费 / (1 + {opts.vat_rate_pct:.1f}%)", f"=ROUND(D4/(1+{vat_rate:.4f}),2)" if opts.include_formula else excl_tax_raw, "muted", excl_tax_raw / grand_raw, "仅作税务口径披露，不重复合计"),
            ("-", "  其中：增值税销项税额", f"一般计税方法 ({opts.vat_rate_pct:.1f}%)", "=ROUND(D4-D7,2)" if opts.include_formula else tax_raw, "muted", tax_raw / grand_raw, "仅作税务口径披露，不重复合计"),
            ("二", "第二部分 工程建设其他费用（独立费用）", f"第一部分工程费用 × {opts.other_project_rate_pct:.2f}%", "=SUM(D10:D13)" if opts.include_formula else other_raw, "section", other_raw / grand_raw, "按现行行业取费标准测算"),
            ("2.1", "建设单位管理费", f"工程费用 × {opts.other_project_rate_pct * 0.20:.2f}%", f"=ROUND(D4*{o_rate * 0.20:.5f},2)" if opts.include_formula else round(other_raw * 0.20, 2), "body", other_raw * 0.20 / grand_raw, ""),
            ("2.2", "工程勘察与设计费", f"工程费用 × {opts.other_project_rate_pct * 0.40:.2f}%", f"=ROUND(D4*{o_rate * 0.40:.5f},2)" if opts.include_formula else round(other_raw * 0.40, 2), "body", other_raw * 0.40 / grand_raw, ""),
            ("2.3", "工程建设监理费", f"工程费用 × {opts.other_project_rate_pct * 0.18:.2f}%", f"=ROUND(D4*{o_rate * 0.18:.5f},2)" if opts.include_formula else round(other_raw * 0.18, 2), "body", other_raw * 0.18 / grand_raw, ""),
            ("2.4", "专项评估、招投标及造价咨询费", f"工程费用 × {opts.other_project_rate_pct * 0.22:.2f}%", f"=ROUND(D4*{o_rate * 0.22:.5f},2)" if opts.include_formula else round(other_raw * 0.22, 2), "body", other_raw * 0.22 / grand_raw, ""),
            ("一+二", "一至二部分投资合计", "第一部分工程费用 + 第二部分其他费用", "=D4+D9" if opts.include_formula else part12_raw, "section", part12_raw / grand_raw, ""),
            ("三", "第三部分 基本预备费", f"(一至二部分合计) × {opts.contingency_rate_pct:.2f}%", f"=ROUND(D14*{c_rate:.4f},2)" if opts.include_formula else cont_raw, "body", cont_raw / grand_raw, "应对设计变更与不可预见风险"),
            ("总计", f"工程静态总投资总计（{u_lbl}）", "第一部分 + 第二部分 + 第三部分", "=D14+D15" if opts.include_formula else grand_raw, "total", 1.0, "可直接用于立项与三级校审报批"),
        ]

        for offset, (seq, name, basis, amt_val, style_kind, pct_val, note) in enumerate(rows_spec):
            r_idx = 4 + offset
            ws.cell(r_idx, 1, seq)
            ws.cell(r_idx, 2, name)
            ws.cell(r_idx, 3, basis)
            ws.cell(r_idx, 4, amt_val)
            pct_cell = f"=IF(D${grand_row}>0,D{r_idx}/D${grand_row},0)" if opts.include_formula else round(pct_val, 4)
            ws.cell(r_idx, 5, pct_cell)
            ws.cell(r_idx, 6, note)

            for c_i in range(1, 7):
                c = ws.cell(r_idx, c_i)
                c.border = self._BORDER
                if style_kind == "total":
                    c.fill = self._TOT_FILL
                    c.font = self._TOT_FONT
                elif style_kind == "section":
                    c.fill = self._SEC_FILL
                    c.font = self._SEC_FONT
                elif style_kind == "muted":
                    c.font = self._MUTED_FONT
                else:
                    c.font = self._BODY_FONT

                if c_i == 4:
                    c.number_format = "#,##0.00"
                    c.alignment = Alignment(horizontal="right", vertical="center")
                elif c_i == 5:
                    c.number_format = "0.00%"
                    c.alignment = Alignment(horizontal="right", vertical="center")
                elif c_i == 1:
                    c.alignment = Alignment(horizontal="center", vertical="center")
                else:
                    c.alignment = Alignment(horizontal="left", vertical="center")

        sign_row = grand_row + 2
        ws.cell(
            sign_row,
            1,
            f"编制人：____________    校核人：____________    审查人（总工）：____________    编制日期：{date.today().isoformat()}",
        )
        ws.merge_cells(start_row=sign_row, start_column=1, end_row=sign_row, end_column=6)
        ws.cell(sign_row, 1).font = Font(name="Microsoft YaHei", size=9.5, color="475569")
        ws.cell(sign_row, 1).alignment = Alignment(horizontal="right", vertical="center")

        for idx, w in enumerate([10, 34, 34, 18, 15, 28], start=1):
            ws.column_dimensions[get_column_letter(idx)].width = w

    def _build_measure_sheet(
        self,
        ws: openpyxl.worksheet.worksheet.Worksheet,
        project: CanonicalProject,
        opts: ExportOptions,
        div_total_row: int,
        div_amount_raw: float,
    ) -> int:
        u_lbl = opts.currency_unit
        ws.append([f"{project.context.project_name} — 措施项目与取费测算明细表"])
        ws.merge_cells("A1:G1")
        ws["A1"].font = Font(name="Microsoft YaHei", size=13, bold=True, color="1E3A5F")
        ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 28

        headers = ["序号", "措施项目名称", "计算基数名称", f"计算基数（{u_lbl}）", "费率 (%)", f"金额（{u_lbl}）", "说明"]
        ws.append(headers)
        for c_i in range(1, 8):
            c = ws.cell(2, c_i)
            c.fill = self._HDR_FILL
            c.font = self._HDR_FONT
            c.alignment = Alignment(horizontal="center", vertical="center")
            c.border = self._BORDER

        div_ref = f"='工程量清单估算表'!H{div_total_row}"
        total_pct = max(0.0, opts.measure_rate_pct)
        breakdown = [
            ("1", "安全文明施工措施费（含环境保护、临时设施）", "分部分项工程费", round(total_pct * 0.55, 2), "强制性不可竞争措施费用"),
            ("2", "冬雨季施工及夜间施工增加费", "分部分项工程费", round(total_pct * 0.20, 2), "结合地区季节气候与工期测算"),
            ("3", "二次搬运、已完工程保护与大型机械进出场", "分部分项工程费", round(total_pct - round(total_pct * 0.55, 2) - round(total_pct * 0.20, 2), 2), "按施工组织条件测算"),
        ]
        for idx, (seq, name, base_name, pct, note) in enumerate(breakdown, start=3):
            ws.cell(idx, 1, seq)
            ws.cell(idx, 2, name)
            ws.cell(idx, 3, base_name)
            ws.cell(idx, 4, div_ref if opts.include_formula else div_amount_raw)
            ws.cell(idx, 5, pct)
            ws.cell(idx, 6, f"=ROUND(D{idx}*E{idx}/100,2)" if opts.include_formula else round(div_amount_raw * pct / 100.0, 2))
            ws.cell(idx, 7, note)
            for c_i in range(1, 8):
                c = ws.cell(idx, c_i)
                c.font = self._BODY_FONT
                c.border = self._BORDER
                if c_i in (4, 5, 6):
                    c.number_format = "#,##0.00"
                    c.alignment = Alignment(horizontal="right", vertical="center")
                elif c_i == 1:
                    c.alignment = Alignment(horizontal="center", vertical="center")

        tot_row = 6
        ws.cell(tot_row, 2, f"措施项目费合计（{u_lbl}）")
        ws.cell(tot_row, 5, "=SUM(E3:E5)" if opts.include_formula else round(total_pct, 2))
        ws.cell(tot_row, 6, "=SUM(F3:F5)" if opts.include_formula else round(div_amount_raw * total_pct / 100.0, 2))
        for c_i in range(1, 8):
            c = ws.cell(tot_row, c_i)
            c.fill = self._SEC_FILL
            c.font = self._SEC_FONT
            c.border = self._BORDER
            if c_i in (5, 6):
                c.number_format = "#,##0.00"
                c.alignment = Alignment(horizontal="right", vertical="center")

        for idx, w in enumerate([8, 36, 18, 18, 12, 18, 30], start=1):
            ws.column_dimensions[get_column_letter(idx)].width = w
        return tot_row

    def _build_estimate_sheet(
        self,
        ws: openpyxl.worksheet.worksheet.Worksheet,
        project: CanonicalProject,
        opts: ExportOptions,
        details_map: dict[str, dict[str, Any]],
    ) -> tuple[int, float]:
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
        div = 10000.0 if opts.currency_unit == "万元" else 1.0

        for item in project.items:
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

        div_amount_raw = round(
            sum((it.normalized_quantity or 0.0) * (it.normalized_unit_price_yuan or 0.0) for it in project.items) / div,
            2,
        )
        ws.cell(row_cursor, 3, f"分部分项工程合价总计（{opts.currency_unit}）")
        if opts.include_formula and leaf_rows:
            ws.cell(row_cursor, 8, f"=SUM(H{leaf_rows[0]}:H{leaf_rows[-1]})")
        else:
            ws.cell(row_cursor, 8, div_amount_raw)

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
        return row_cursor, div_amount_raw

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
