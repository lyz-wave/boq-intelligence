from typing import Any, Protocol, runtime_checkable

from src.core.contracts.boq_item import FeatureSlotValue, RawBoqItem


@runtime_checkable
class FeatureExtractorPort(Protocol):
    """接缝 2：语义特征槽位抽取插件接口"""

    extractor_id: str
    priority: int  # 10=正则本体优先, 90=LLM强类型兜底

    def extract(self, raw_item: RawBoqItem, ontology: dict[str, Any]) -> dict[str, FeatureSlotValue]:
        ...
