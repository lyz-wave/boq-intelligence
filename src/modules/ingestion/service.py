from contextlib import nullcontext
import hashlib
from pathlib import Path
from typing import Any, Optional

import xlrd

from src.core.contracts.boq_item import CanonicalProject, RawBoqItem, StandardizedBoqItem
from src.core.contracts.project import (
    CostStage,
    EngineeringDomain,
    PartialContext,
    ProjectContext,
    ProjectFileBundle,
)
from src.core.ports.parser_port import DocumentParserPort
from src.core.registry import PluginRegistry
from src.infrastructure.llm_client import LlmClientManager
from src.modules.ingestion import adapters as _loaded_adapters  # noqa: F401


class IngestionModule:
    """文档摄入深模块：对外仅暴露 ingest(bundle) -> CanonicalProject 接口."""

    def __init__(
        self,
        standardizer: Optional[Any] = None,
        auditor: Optional[Any] = None,
        parsers: Optional[list[DocumentParserPort]] = None,
    ) -> None:
        self._standardizer = standardizer
        self._auditor = auditor
        self._parsers = parsers or PluginRegistry.instantiate_all("parsers")

    def ingest(self, bundle: ProjectFileBundle) -> CanonicalProject:
        if not bundle.files:
            raise ValueError("ProjectFileBundle.files cannot be empty.")

        partial_contexts: list[PartialContext] = []
        all_raw_items: list[RawBoqItem] = []

        for fpath in bundle.files:
            parser = self._select_parser(fpath)
            p_ctx, raw_items = parser.parse(fpath, bundle)
            partial_contexts.append(p_ctx)
            all_raw_items.extend(raw_items)

        project_ctx = self._merge_contexts(partial_contexts, bundle)
        leaf_items = [it for it in all_raw_items if not it.is_section_header]
        header_items_count = len(all_raw_items) - len(leaf_items)

        std_module = self._get_standardizer()
        scope = LlmClientManager.get_instance().offline_scope() if len(leaf_items) > 15 else nullcontext()
        with scope:
            standardized_items: list[StandardizedBoqItem] = [
                std_module.standardize(
                    item=raw_it,
                    domain=project_ctx.domain.value,
                    project_id=project_ctx.project_id,
                    idx=idx + 1,
                )
                for idx, raw_it in enumerate(leaf_items)
            ]

        quality_metrics = self._compute_quality_metrics(project_ctx, standardized_items)
        project = CanonicalProject(
            context=project_ctx,
            items=standardized_items,
            raw_items_count=len(leaf_items),
            header_items_count=header_items_count,
            quality_metrics=quality_metrics,
        )

        if self._auditor is not None:
            project.audit_findings = self._auditor.audit(project)

        return project

    def _select_parser(self, file_path: Path) -> DocumentParserPort:
        preview = self._read_header_preview(file_path)
        for parser in self._parsers:
            if parser.can_handle(file_path, preview):
                return parser
        raise ValueError(f"No registered DocumentParserPort can handle file: {file_path}")

    def _read_header_preview(self, file_path: Path) -> list[str]:
        if file_path.suffix.lower() != ".xls":
            return [file_path.name]
        try:
            wb = xlrd.open_workbook(str(file_path), on_demand=True)
            sh = wb.sheet_by_index(0)
            lines: list[str] = list(wb.sheet_names())
            for r in range(min(5, sh.nrows)):
                row_vals = [str(sh.cell_value(r, c)).strip() for c in range(min(10, sh.ncols)) if sh.cell_value(r, c)]
                if row_vals:
                    lines.append(" ".join(row_vals))
            wb.release_resources()
            return lines
        except Exception:
            return [file_path.name]

    def _merge_contexts(
        self, partials: list[PartialContext], bundle: ProjectFileBundle
    ) -> ProjectContext:
        # 优先采用 Word 上下文信息，再用表内数据补全
        word_first = sorted(partials, key=lambda p: 0 if p.source_file.lower().endswith((".doc", ".docx")) else 1)

        project_name = bundle.project_name
        domain = bundle.domain_hint
        province = bundle.province_hint
        city = bundle.city_hint
        district = bundle.district_hint
        stage = bundle.stage_hint
        month = bundle.price_base_month_hint
        quotas: list[str] = []
        labor: dict[str, float] = {}
        utilities: dict[str, float] = {}
        fee_rules = []
        materials = []
        total_inv: Optional[float] = None
        sources = [p.source_file for p in partials]

        for p in word_first:
            project_name = project_name or p.project_name
            domain = domain or p.domain
            province = province or p.province
            city = city or p.city
            district = district or p.district
            stage = stage or p.stage
            month = month or p.price_base_month
            for q in p.quota_standards:
                if q not in quotas:
                    quotas.append(q)
            labor.update(p.labor_rates)
            utilities.update(p.utility_rates)
            if p.fee_rules:
                fee_rules.extend(p.fee_rules)
            if p.base_materials:
                materials.extend(p.base_materials)
            if total_inv is None and p.total_investment_yuan is not None:
                total_inv = p.total_investment_yuan

        final_name = project_name or (bundle.files[0].stem if bundle.files else "未命名工程项目")
        pid = "proj_" + hashlib.md5(final_name.encode("utf-8")).hexdigest()[:10]
        return ProjectContext(
            project_id=pid,
            project_name=final_name,
            domain=domain or EngineeringDomain.WATER_ECOLOGY,
            province=province or "未指定",
            city=city,
            district=district,
            stage=stage or CostStage.PRELIMINARY_DESIGN,
            price_base_month=month or "2026-01",
            quota_standards=quotas,
            labor_rates=labor,
            utility_rates=utilities,
            fee_rules=fee_rules,
            base_materials=materials,
            total_investment_yuan=total_inv,
            source_files=sources,
        )

    def _get_standardizer(self) -> Any:
        if self._standardizer is None:
            from src.modules.standardizer.service import SemanticStandardizerModule

            self._standardizer = SemanticStandardizerModule()
        return self._standardizer

    def _compute_quality_metrics(
        self, ctx: ProjectContext, items: list[StandardizedBoqItem]
    ) -> dict[str, float]:
        if not items:
            return {"completeness": 0.0, "accuracy": 0.0, "consistency": 1.0, "uniqueness": 1.0, "overall": 0.0}
        complete_rows = sum(
            1 for it in items if it.standard_name and it.standard_unit and it.normalized_unit_price_yuan is not None
        )
        completeness = round(complete_rows / len(items), 4)

        checked, closed = 0, 0
        for it in items:
            if it.normalized_quantity is not None and it.normalized_unit_price_yuan is not None and it.total_price_yuan is not None:
                checked += 1
                expected = it.normalized_quantity * it.normalized_unit_price_yuan
                tol = max(abs(expected) * 0.015, 50.0)  # 兼容万元保留两位小数的舍入误差
                if abs(expected - it.total_price_yuan) <= tol:
                    closed += 1
        accuracy = round(closed / checked, 4) if checked else 1.0

        seen = set()
        dup = 0
        for it in items:
            sig = (tuple(it.section_path), it.raw_text_full, it.standard_unit)
            if sig in seen:
                dup += 1
            seen.add(sig)
        uniqueness = round(1.0 - dup / len(items), 4)
        overall = round(0.4 * completeness + 0.4 * accuracy + 0.2 * uniqueness, 4)
        return {
            "completeness": completeness,
            "accuracy": accuracy,
            "uniqueness": uniqueness,
            "overall": overall,
        }
