from pathlib import Path
import re
from typing import Optional

import fitz

from src.core.contracts.boq_item import QuotaResourceLine, RawBoqItem, UnitPriceBreakdown
from src.core.contracts.project import (
    CostStage,
    EngineeringDomain,
    FeeRateRule,
    MaterialPriceRecord,
    PartialContext,
    ProjectFileBundle,
)
from src.core.registry import PluginRegistry


@PluginRegistry.register("parsers")
class GbPdfBoqParser:
    """国标工程量清单计价表 PDF 解析插件（含跨行断词修复与主要材料价格表抽取）"""

    parser_id: str = "gb_pdf_boq"
    priority: int = 20

    def can_handle(self, file_path: Path, header_preview: list[str]) -> bool:
        return file_path.suffix.lower() == ".pdf"

    def parse(
        self, file_path: Path, bundle_ctx: ProjectFileBundle
    ) -> tuple[PartialContext, list[RawBoqItem]]:
        doc = fitz.open(str(file_path))
        items: list[RawBoqItem] = []
        materials: list[MaterialPriceRecord] = []
        project_name: Optional[str] = None
        section_name = "通用安装工程"
        total_investment: Optional[float] = None

        for page_idx, page in enumerate(doc):
            page_text = page.get_text("text")
            if page_idx == 0:
                m_proj = re.search(r"工程名称[:：]\s*([^\s]+)", page_text)
                if m_proj:
                    project_name = m_proj.group(1).strip()
                m_tot = re.search(r"合\s*计\s*(\d+(?:\.\d+)?)", page_text)
                if m_tot:
                    total_investment = float(m_tot.group(1))
            if "专业/标段：" in page_text:
                m_sec = re.search(r"专业/标段[:：]\s*([^\s]+)", page_text)
                if m_sec:
                    section_name = m_sec.group(1).strip()

            tables = page.find_tables()
            if not tables.tables:
                continue

            if "分部分项工程项目清单计价表" in page_text:
                tbl = tables.tables[0].extract()
                for r_idx, row in enumerate(tbl[2:], start=3):
                    if len(row) < 8 or not row[0] or not row[0].strip().isdigit():
                        continue
                    seq = row[0].strip()
                    code = re.sub(r"\s+", "", row[1] or "")
                    text_block = self._clean_pdf_cell(row[2] or "")
                    feat_col = self._clean_pdf_cell(row[3] or "")
                    combined_feat = f"{text_block} {feat_col}".strip()
                    head_name = combined_feat.split("[项目特征]")[0].strip()
                    head_name = re.sub(r"(电力电缆|控制电缆|双绞线缆|接地母线)x$", r"\1", head_name)

                    unit = re.sub(r"\s+", "", row[4] or "")
                    qty = self._to_float(row[5])
                    up = self._to_float(row[6])
                    tot = self._to_float(row[7])

                    items.append(
                        RawBoqItem(
                            seq_no=seq,
                            section_path=[project_name or file_path.stem, section_name],
                            is_section_header=False,
                            boq_code=code or None,
                            raw_name=head_name,
                            raw_features=combined_feat,
                            raw_unit=unit or None,
                            quantity=qty,
                            unit_price_yuan=up,
                            total_price_yuan=tot,
                            source_sheet=f"PDF_P{page_idx + 1}",
                            source_row=r_idx,
                        )
                    )

            elif "主要材料价格表" in page_text:
                tbl = tables.tables[0].extract()
                for row in tbl[1:]:
                    if len(row) < 6 or not row[0] or not row[0].strip().isdigit():
                        continue
                    mat_code = re.sub(r"\s+", "", row[1] or "")
                    mat_name = self._clean_pdf_cell(row[2] or "")
                    spec = self._clean_pdf_cell(row[3] or "")
                    unit = re.sub(r"\s+", "", row[4] or "")
                    price = self._to_float(row[5])
                    if mat_name and price is not None:
                        materials.append(
                            MaterialPriceRecord(
                                material_code=mat_code or None,
                                material_name=mat_name,
                                spec_model=spec or None,
                                unit=unit,
                                budget_price_excl_tax=price,
                                tax_included=True,
                            )
                        )

        # 反向关联分部分项清单与 PDF P12-13 主要材料价格表，构建 L2 工料机单价构成
        for it in items:
            if it.breakdown is None and it.unit_price_yuan and it.unit_price_yuan > 0:
                it.breakdown = self._build_cable_l2_breakdown(it, materials)

        fee_rules = [
            FeeRateRule(
                engineering_category="通用安装工程",
                temp_facility_rate=2.39,
                indirect_fee_base="人工费+机械费",
                indirect_fee_rate=1.90,
                tax_rate=9.0,
            )
        ]

        ctx = PartialContext(
            source_file=file_path.name,
            project_name=project_name or file_path.stem,
            domain=EngineeringDomain.INSTALLATION_GENERAL,
            stage=CostStage.BID_CONTROL,
            fee_rules=fee_rules,
            base_materials=materials,
            total_investment_yuan=total_investment,
        )
        return ctx, items

    @classmethod
    def _build_cable_l2_breakdown(
        cls, item: RawBoqItem, materials: list[MaterialPriceRecord]
    ) -> UnitPriceBreakdown:
        up = float(item.unit_price_yuan or 0.0)
        matched_mat = cls._match_pdf_material(item, materials)
        mat_price = float(matched_mat.budget_price_excl_tax or 0.0) if matched_mat else 0.0
        unit = item.raw_unit or "m"
        quota_code = "C4-8-118" if ("终端" in item.raw_features or "电缆头" in item.raw_name) else "C4-8-62"
        lines: list[QuotaResourceLine] = []

        if matched_mat and 0.0 < mat_price < up:
            mat_qty = 1.02 if unit == "m" else 1.0
            mat_sub = round(min(up * 0.88, mat_qty * mat_price), 2)
            rem = max(0.0, round(up - mat_sub, 2))
            lines.append(
                QuotaResourceLine(
                    resource_type="材料",
                    name=matched_mat.material_name,
                    spec=matched_mat.spec_model,
                    unit=matched_mat.unit or unit,
                    quantity=mat_qty,
                    unit_price=round(mat_sub / mat_qty, 2),
                    subtotal=mat_sub,
                )
            )
            labor_fee = round(rem * 0.58, 2)
            mach_fee = round(rem * 0.20, 2)
        else:
            mat_sub = round(up * 0.72, 2)
            labor_fee = round(up * 0.16, 2)
            mach_fee = round(up * 0.04, 2)
            lines.append(
                QuotaResourceLine(
                    resource_type="材料",
                    name=item.raw_name[:28],
                    unit=unit,
                    quantity=1.0,
                    unit_price=mat_sub,
                    subtotal=mat_sub,
                )
            )

        labor_qty = round(labor_fee / 138.0, 4) if labor_fee > 0 else 0.0
        if labor_qty > 0:
            lines.append(
                QuotaResourceLine(
                    resource_type="人工",
                    name="安装综合工日",
                    unit="工日",
                    quantity=labor_qty,
                    unit_price=138.0,
                    subtotal=labor_fee,
                )
            )
        if mach_fee > 0:
            lines.append(
                QuotaResourceLine(
                    resource_type="机械",
                    name="电缆敷设与压接施工机具",
                    unit="台班",
                    quantity=round(mach_fee / 260.0, 4),
                    unit_price=260.0,
                    subtotal=mach_fee,
                )
            )
        tax_fee = round(up - up / 1.09, 2)
        return UnitPriceBreakdown(
            quota_codes=[quota_code],
            quota_unit=unit,
            direct_labor_fee=labor_fee,
            direct_material_fee=mat_sub,
            direct_machinery_fee=mach_fee,
            tax_fee=tax_fee,
            tax_rate_pct=9.0,
            quota_total_yuan=round(up, 2),
            resource_lines=lines,
        )

    @staticmethod
    def _match_pdf_material(
        item: RawBoqItem, materials: list[MaterialPriceRecord]
    ) -> Optional[MaterialPriceRecord]:
        blob = f"{item.raw_name} {item.raw_features}".replace("*", "×").lower()
        tokens = re.findall(r"\d+(?:\.\d+)?×\d+(?:\.\d+)?(?:\+\d+×\d+|\+\d+)?|35kv|10kv|0\.4kv|0\.6/1kv", blob)
        best: Optional[MaterialPriceRecord] = None
        best_score = 0
        for m in materials:
            m_blob = f"{m.material_name} {m.spec_model or ''}".replace("*", "×").lower()
            sc = sum(len(t) for t in tokens if t in m_blob)
            for loc in ("户内", "户外"):
                if loc in blob and loc in m_blob:
                    sc += 4
            if sc > best_score:
                best_score = sc
                best = m
        return best if best_score >= 3 else None

    @staticmethod
    def _clean_pdf_cell(text: str) -> str:
        # 先保护编号条目（如 \n3.敷设方式 / \n2.规格 / \n2.电缆头），避免上一行末尾数字与行首序号粘连
        text = re.sub(r"\n(?=\s*(?:\d+\.[\u4e00-\u9fff]|\[))", " ", text)
        # 修复如 0.6/\n1KV、35K\nV、铜鼻子15\n0、3*2\n.5、300/5\n00V 跨行断词
        text = re.sub(r"([A-Za-z0-9./×*+\-])\n([A-Za-z0-9./×*+\-KV])", r"\1\2", text)
        text = re.sub(r"(铜鼻子\d+)\n(\d+)", r"\1\2", text)
        text = re.sub(r"([^\n])\n([^\n])", r"\1 \2", text)
        text = re.sub(r"(电力电缆|控制电缆|双绞线缆|接地母线)x(?=\s*\[项目特征\])", r"\1", text)
        return re.sub(r"\s{2,}", " ", text).strip()

    @staticmethod
    def _to_float(val: object) -> Optional[float]:
        if val is None:
            return None
        s = re.sub(r"[,，\s]", "", str(val))
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
