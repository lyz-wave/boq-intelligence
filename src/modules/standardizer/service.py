import hashlib
from pathlib import Path
import re
from typing import Any, Optional

import yaml

from src.core.contracts.boq_item import FeatureSlotValue, RawBoqItem, StandardizedBoqItem
from src.core.paths import get_bundle_root
from src.core.ports.extractor_port import FeatureExtractorPort
from src.core.registry import PluginRegistry
from src.infrastructure.llm_client import LlmClientManager
from src.modules.standardizer import adapters as _loaded_extractors  # noqa: F401
from src.modules.standardizer.adapters.yaml_regex_extractor import YamlRegexFeatureExtractor
from src.modules.standardizer.unit_converter import normalize_engineering_text, normalize_unit


class SemanticStandardizerModule:
    """语义标准化深模块：对外仅暴露 standardize(item, domain) -> StandardizedBoqItem 接口."""

    def __init__(
        self,
        domains_dir: Optional[Path] = None,
        extractors: Optional[list[FeatureExtractorPort]] = None,
    ) -> None:
        self._domains_dir = domains_dir or get_bundle_root() / "configs" / "domains"
        self._ontologies: dict[str, dict[str, Any]] = self._load_ontologies()
        self._extractors = extractors or PluginRegistry.instantiate_all("extractors")

    def _load_ontologies(self) -> dict[str, dict[str, Any]]:
        loaded: dict[str, dict[str, Any]] = {}
        if not self._domains_dir.exists():
            return loaded
        for yfile in sorted(self._domains_dir.glob("*.yaml")):
            with open(yfile, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            code = data.get("domain_code")
            if code:
                loaded[code] = data
        return loaded

    def get_ontology(self, domain: str) -> dict[str, Any]:
        return self._ontologies.get(domain, {})

    def list_ontologies(self) -> dict[str, dict[str, Any]]:
        return dict(self._ontologies)

    def standardize(
        self,
        item: RawBoqItem,
        domain: str,
        project_id: str = "query",
        idx: int = 1,
    ) -> StandardizedBoqItem:
        ontology = self._ontologies.get(domain, {})
        # 若指定 domain 下未匹配到分类，则跨领域嗅探最贴合的本体（用于查询或混合项目）
        category_code = f"{domain}_general"
        matched_ontology = ontology

        regex_ext = next((e for e in self._extractors if isinstance(e, YamlRegexFeatureExtractor)), None)
        if regex_ext:
            cat = regex_ext.match_category(item, ontology) if ontology else None
            if not cat:
                for cand_dom, cand_ont in self._ontologies.items():
                    cand_cat = regex_ext.match_category(item, cand_ont)
                    if cand_cat:
                        cat = cand_cat
                        matched_ontology = cand_ont
                        break
            if cat:
                category_code = cat["category_code"]

        llm_always = (
            LlmClientManager.get_instance().is_active()
            and LlmClientManager.get_instance().settings.trigger_mode == "always"
        )
        slots: dict[str, FeatureSlotValue] = {}
        for extractor in self._extractors:
            # Tier-1 优先抽取；Tier-2 默认仅在正则表达式未抽满时兜底（配置 always 时协同增强）
            if extractor.priority >= 90 and len(slots) >= 2 and not llm_always:
                continue
            extracted = extractor.extract(item, matched_ontology)
            for k, v in extracted.items():
                if k not in slots:
                    slots[k] = v

        std_unit, factor = normalize_unit(item.raw_unit, matched_ontology)
        norm_qty = round(item.quantity * factor, 4) if item.quantity is not None else None
        norm_up = (
            round(item.unit_price_yuan / factor, 4)
            if (item.unit_price_yuan is not None and factor > 0)
            else item.unit_price_yuan
        )

        std_name = self._normalize_standard_name(item.raw_name, matched_ontology, slots)
        text_parts = [item.raw_name]
        if item.raw_features and item.raw_features not in item.raw_name:
            text_parts.append(item.raw_features)
        if item.raw_work_content and item.raw_work_content not in " ".join(text_parts):
            text_parts.append(item.raw_work_content)
        raw_full = normalize_engineering_text(" ".join(p for p in text_parts if p))
        search_parts = list(dict.fromkeys([*item.section_path, std_name, raw_full]))
        canonical_search = normalize_engineering_text(" | ".join(p for p in search_parts if p))

        sig = f"{project_id}:{idx}:{item.seq_no}:{raw_full[:40]}"
        item_id = "boq_" + hashlib.md5(sig.encode("utf-8")).hexdigest()[:12]

        return StandardizedBoqItem(
            item_id=item_id,
            project_id=project_id,
            seq_no=item.seq_no,
            domain=domain,
            category_code=category_code,
            standard_name=std_name,
            boq_code=item.boq_code,
            section_path=item.section_path,
            raw_text_full=raw_full,
            canonical_search_text=canonical_search,
            standard_unit=std_unit,
            raw_unit=item.raw_unit,
            unit_conversion_factor=factor,
            normalized_quantity=norm_qty,
            normalized_unit_price_yuan=norm_up,
            raw_unit_price_yuan=item.unit_price_yuan,
            total_price_yuan=item.total_price_yuan,
            slots=slots,
            breakdown=item.breakdown,
            quality_score=1.0,
        )

    @staticmethod
    def _normalize_standard_name(
        raw_name: str, ontology: dict[str, Any], slots: dict[str, FeatureSlotValue]
    ) -> str:
        clean = normalize_engineering_text(raw_name).split("规格:")[0].split("[项目特征]")[0].strip()
        synonyms: dict[str, str] = ontology.get("name_synonyms", {})
        for kw, target in synonyms.items():
            if kw in clean:
                return target
        # 若首词过长（如混入了参数），精简为前段主词
        if len(clean) > 22:
            head = re.split(r"[,，(（\s]", clean)[0]
            if len(head) >= 2:
                return head
        return clean or "未命名清单项"
