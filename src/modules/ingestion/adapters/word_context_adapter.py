from pathlib import Path
import re
import subprocess
from typing import Optional

import docx

from src.core.contracts.boq_item import RawBoqItem
from src.core.contracts.project import (
    CostStage,
    EngineeringDomain,
    FeeRateRule,
    MaterialPriceRecord,
    PartialContext,
    ProjectFileBundle,
)
from src.core.registry import PluginRegistry

_PROVINCE_CITY_MAP = [
    ("内蒙古", "内蒙古自治区", "鄂尔多斯市", "达拉特旗"),
    ("达拉特旗", "内蒙古自治区", "鄂尔多斯市", "达拉特旗"),
    ("佛山市", "广东省", "佛山市", "高明区"),
    ("高明区", "广东省", "佛山市", "高明区"),
    ("西安市", "陕西省", "西安市", None),
    ("长安区", "陕西省", "西安市", "长安区"),
    ("广东省", "广东省", None, None),
    ("陕西省", "陕西省", None, None),
]


def _extract_region(text: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    prov, city, dist = None, None, None
    for kw, p, c, d in _PROVINCE_CITY_MAP:
        if kw in text:
            prov = prov or p
            city = city or c
            dist = dist or d
    return prov, city, dist


def _extract_domain(text: str) -> Optional[EngineeringDomain]:
    if any(k in text for k in ("全域土地综合整治", "耕地恢复", "垦造水田", "土地开发整理")):
        return EngineeringDomain.LAND_CONSOLIDATION
    if any(k in text for k in ("生态综合治理", "滩地生态修复", "水环境", "生态护坡", "清淤")):
        return EngineeringDomain.WATER_ECOLOGY
    if any(k in text for k in ("泵站电缆", "通用安装", "电力电缆")):
        return EngineeringDomain.INSTALLATION_GENERAL
    if any(k in text for k in ("给排水", "污水处理厂", "生活污水")):
        return EngineeringDomain.URBAN_WATER
    if any(k in text for k in ("灌区", "渠道衬砌")):
        return EngineeringDomain.IRRIGATION
    if any(k in text for k in ("疏干水", "矿井水")):
        return EngineeringDomain.COAL_MINE_WATER
    if any(k in text for k in ("海洋生态", "红树林")):
        return EngineeringDomain.MARINE_ECOLOGY
    return None


def _extract_stage(text: str) -> Optional[CostStage]:
    for stage in CostStage:
        if stage.value in text:
            return stage
    return None


def _extract_price_month(text: str) -> Optional[str]:
    for line in text.splitlines():
        if any(k in line for k in ("信息价", "造价信息", "动态人工", "材料价格", "材料价")):
            m = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月", line)
            if m:
                return f"{m.group(1)}-{int(m.group(2)):02d}"
    m = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    return None


def _extract_quotas(text: str) -> list[str]:
    quotas: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if re.match(r"^[（(]\d+[）)]", s) and ("定额" in s or "规范" in s or "规定" in s or "标准" in s or "办法" in s or "指南" in s):
            quotas.append(re.sub(r"^[（(]\d+[）)]\s*", "", s).rstrip("；;。"))
    if not quotas:
        for m in re.findall(r"《[^》]+》(?:（[^）]+）|〔\d+〕\d+号)?", text):
            if any(k in m for k in ("定额", "规范", "编制规定", "指南", "标准")):
                if m not in quotas:
                    quotas.append(m)
    return quotas[:15]


def _extract_labor_and_utilities(text: str) -> tuple[dict[str, float], dict[str, float]]:
    labor: dict[str, float] = {}
    for label in ("工长", "高级工", "中级工", "初级工", "甲类", "乙类"):
        m = re.search(rf"{label}[工]?\s*(\d+(?:\.\d+)?)\s*(元/工[时日])", text)
        if m:
            labor[label] = float(m.group(1))
    utilities: dict[str, float] = {}
    for key, kw in [("电", "施工用电"), ("风", "施工用风"), ("水", "施工用水")]:
        m = re.search(rf"{kw}[^。\n]*?(\d+(?:\.\d+)?)\s*元/", text)
        if m:
            utilities[key] = float(m.group(1))
    return labor, utilities


def _extract_investment_yuan(text: str) -> Optional[float]:
    m = re.search(r"工程总投资\s*(\d+(?:\.\d+)?)\s*万元", text)
    if m:
        return round(float(m.group(1)) * 10000.0, 2)
    m2 = re.search(r"工程费用为\s*(\d+(?:\.\d+)?)\s*元", text)
    if m2:
        return round(float(m2.group(1)), 2)
    return None


@PluginRegistry.register("parsers")
class WordContextParser:
    """Word 编制说明解析插件（支持 .docx 与 .doc）"""

    parser_id: str = "word_context_parser"
    priority: int = 10

    def can_handle(self, file_path: Path, header_preview: list[str]) -> bool:
        return file_path.suffix.lower() in {".docx", ".doc"}

    def parse(
        self, file_path: Path, bundle_ctx: ProjectFileBundle
    ) -> tuple[PartialContext, list[RawBoqItem]]:
        if file_path.suffix.lower() == ".docx":
            text, materials, fee_rules = self._parse_docx(file_path)
        else:
            text, materials, fee_rules = self._parse_doc_via_textutil(file_path)

        prov, city, dist = _extract_region(text)
        for m in materials:
            m.region = dist or city or prov
        month = _extract_price_month(text)
        if month:
            for m in materials:
                m.price_month = month

        labor, utilities = _extract_labor_and_utilities(text)
        project_name = self._infer_project_name(file_path, text)

        ctx = PartialContext(
            source_file=file_path.name,
            project_name=project_name,
            domain=_extract_domain(text),
            province=prov,
            city=city,
            district=dist,
            stage=_extract_stage(text),
            price_base_month=month,
            quota_standards=_extract_quotas(text),
            labor_rates=labor,
            utility_rates=utilities,
            fee_rules=fee_rules,
            base_materials=materials,
            total_investment_yuan=_extract_investment_yuan(text),
        )
        return ctx, []

    def _infer_project_name(self, file_path: Path, text: str) -> str:
        for line in text.splitlines():
            s = line.strip()
            if s and any(k in s for k in ("工程", "项目")) and len(s) <= 40:
                return s
        return file_path.stem

    def _parse_docx(self, file_path: Path) -> tuple[str, list[MaterialPriceRecord], list[FeeRateRule]]:
        doc = docx.Document(str(file_path))
        text = "\n".join(p.text.strip() for p in doc.paragraphs if p.text.strip())
        materials: list[MaterialPriceRecord] = []
        fee_rules: list[FeeRateRule] = []

        for tbl in doc.tables:
            rows = [[c.text.strip().replace("\n", " ") for c in row.cells] for row in tbl.rows]
            if not rows:
                continue
            hdr = " ".join(rows[0])
            if "材料名称" in hdr and ("预算价格" in hdr or "规定价格" in hdr):
                for r in rows[1:]:
                    if len(r) >= 5 and r[1]:
                        materials.append(
                            MaterialPriceRecord(
                                material_name=r[1],
                                unit=r[2],
                                base_price=self._to_float(r[3]),
                                budget_price_excl_tax=self._to_float(r[4]),
                            )
                        )
            elif "工程类别" in hdr and "间接费率" in hdr:
                for r in rows[1:]:
                    if len(r) >= 4 and r[1]:
                        fee_rules.append(
                            FeeRateRule(
                                engineering_category=r[1],
                                indirect_fee_base=r[2] or "直接费",
                                indirect_fee_rate=self._to_float(r[3]),
                            )
                        )
        return text, materials, fee_rules

    def _parse_doc_via_textutil(self, file_path: Path) -> tuple[str, list[MaterialPriceRecord], list[FeeRateRule]]:
        res = subprocess.run(
            ["textutil", "-convert", "txt", "-stdout", str(file_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        text = res.stdout
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        materials = self._parse_doc_material_block(lines)
        fee_rules = self._parse_doc_fee_block(lines)
        return text, materials, fee_rules

    def _parse_doc_material_block(self, lines: list[str]) -> list[MaterialPriceRecord]:
        materials: list[MaterialPriceRecord] = []
        i = 0
        while i < len(lines):
            if lines[i] == "基价（元）":
                i += 1
                while i + 4 < len(lines) and lines[i].isdigit():
                    materials.append(
                        MaterialPriceRecord(
                            material_name=lines[i + 1],
                            unit=lines[i + 2],
                            budget_price_excl_tax=self._to_float(lines[i + 3]),
                            base_price=self._to_float(lines[i + 4]),
                        )
                    )
                    i += 5
                break
            i += 1
        return materials

    def _parse_doc_fee_block(self, lines: list[str]) -> list[FeeRateRule]:
        temp_rates: dict[str, float] = {}
        indirect_rules: list[FeeRateRule] = []
        for idx, ln in enumerate(lines):
            if ln == "临时设施费费率（%）":
                j = idx + 1
                while j + 3 < len(lines) and lines[j].isdigit():
                    temp_rates[lines[j + 1]] = self._to_float(lines[j + 3]) or 0.0
                    j += 4
            elif ln == "间接费率（%）":
                j = idx + 1
                while j + 3 < len(lines) and lines[j].isdigit():
                    cat = lines[j + 1]
                    indirect_rules.append(
                        FeeRateRule(
                            engineering_category=cat,
                            temp_facility_rate=temp_rates.get(cat),
                            indirect_fee_base=lines[j + 2],
                            indirect_fee_rate=self._to_float(lines[j + 3]),
                        )
                    )
                    j += 4
        return indirect_rules

    @staticmethod
    def _to_float(val: str) -> Optional[float]:
        try:
            return float(val.strip())
        except (ValueError, AttributeError):
            return None
