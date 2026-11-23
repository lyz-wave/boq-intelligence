import re
from typing import Any

from src.core.contracts.boq_item import FeatureDimension, FeatureSlotValue, RawBoqItem
from src.core.registry import PluginRegistry


@PluginRegistry.register("extractors")
class LlmSchemaFeatureExtractor:
    """Tier-2 受控 Schema 兜底抽取插件（离线环境下采用启发式实体-工艺补充提取，可配置切换 LLM API）"""

    extractor_id: str = "llm_schema_extractor"
    priority: int = 90

    def extract(self, raw_item: RawBoqItem, ontology: dict[str, Any]) -> dict[str, FeatureSlotValue]:
        text = f"{raw_item.raw_name} {raw_item.raw_features} {raw_item.raw_work_content}".strip()
        slots: dict[str, FeatureSlotValue] = {}

        # 补充提取混凝土/砂浆标号
        m_grade = re.search(r"(C\d{2}|M\d+(?:\.\d)?)", text)
        if m_grade:
            slots["concrete_grade"] = FeatureSlotValue(
                slot_key="concrete_grade",
                slot_label="标号等级",
                dimension=FeatureDimension.ENTITY_SPEC,
                raw_text=m_grade.group(1),
                normalized_value=m_grade.group(1),
                numeric_value=float(re.findall(r"\d+", m_grade.group(1))[0]),
                hard_constraint=True,
                ahp_weight=0.25,
                extractor_source="llm_fallback",
            )

        # 补充提取工程通用几何尺寸（如 0.5×0.5m, 600mm×440mm）
        m_dim = re.search(r"(\d+(?:\.\d+)?(?:cm|mm|m)?[×*]\d+(?:\.\d+)?(?:cm|mm|m)?)", text)
        if m_dim:
            slots["geometry_dim"] = FeatureSlotValue(
                slot_key="geometry_dim",
                slot_label="几何断面尺寸",
                dimension=FeatureDimension.ENTITY_SPEC,
                raw_text=m_dim.group(1),
                normalized_value=m_dim.group(1).replace("*", "×"),
                hard_constraint=False,
                ahp_weight=0.15,
                extractor_source="llm_fallback",
            )

        return slots
