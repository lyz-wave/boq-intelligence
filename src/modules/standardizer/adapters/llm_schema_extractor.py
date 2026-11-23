import re
from typing import Any

from src.core.contracts.boq_item import FeatureDimension, FeatureSlotValue, RawBoqItem
from src.core.registry import PluginRegistry
from src.infrastructure.llm_client import LlmClientManager


@PluginRegistry.register("extractors")
class LlmSchemaFeatureExtractor:
    """Tier-2 受控 Schema 兜底抽取插件（支持任意 OpenAI 兼容中转站，离线时自动降级启发式提取）"""

    extractor_id: str = "llm_schema_extractor"
    priority: int = 90

    def extract(self, raw_item: RawBoqItem, ontology: dict[str, Any]) -> dict[str, FeatureSlotValue]:
        text = f"{raw_item.raw_name} {raw_item.raw_features} {raw_item.raw_work_content}".strip()
        slots: dict[str, FeatureSlotValue] = {}

        # 1) 本地离线保底抽取：混凝土/砂浆标号与几何尺寸
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

        # 2) 若启用了第三方大模型中转站，则按受控 JSON Schema 调用并合并补充槽位
        mgr = LlmClientManager.get_instance()
        if mgr.is_active():
            try:
                domain_label = str(ontology.get("domain_name") or ontology.get("domain_code") or "")
                res = mgr.chat_extract_slots(text=text, domain=domain_label)
                for k, v in (res.get("slots") or {}).items():
                    if k not in slots:
                        slots[k] = v
            except Exception:
                # 远端超时或鉴权失败时静默保持离线保底结果，不阻断主流程
                pass

        return slots
