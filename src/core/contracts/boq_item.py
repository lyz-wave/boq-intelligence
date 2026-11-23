from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field

from src.core.contracts.project import ProjectContext


class FeatureDimension(str, Enum):
    ENTITY_SPEC = "entity_spec"      # 实体材质与规格特征
    PROCESS_WORK = "process_work"    # 施工工艺与工作内容特征
    ENV_CONDITION = "env_condition"  # 环境与工况特征


class FeatureSlotValue(BaseModel):
    """结构化特征槽位值"""

    slot_key: str
    slot_label: str
    dimension: FeatureDimension
    raw_text: str
    normalized_value: Any
    numeric_value: Optional[float] = None
    numeric_unit: Optional[str] = None
    hard_constraint: bool = False
    ahp_weight: float = 0.0
    extractor_source: str = "regex"  # "regex" | "llm" | "manual"


class QuotaResourceLine(BaseModel):
    """L2 单价构成中的单条工料机/取费消耗"""

    resource_type: str  # "人工" | "材料" | "机械" | "价差" | "取费"
    name: str
    spec: Optional[str] = None
    unit: str = ""
    quantity: float = 0.0
    unit_price: float = 0.0
    subtotal: float = 0.0


class UnitPriceBreakdown(BaseModel):
    """L2 单价构成分析契约"""

    quota_codes: list[str] = Field(default_factory=list)
    quota_unit: Optional[str] = None
    direct_labor_fee: Optional[float] = None
    direct_material_fee: Optional[float] = None
    direct_machinery_fee: Optional[float] = None
    measure_fee: Optional[float] = None
    measure_rate_pct: Optional[float] = None
    indirect_fee: Optional[float] = None
    indirect_rate_pct: Optional[float] = None
    profit_fee: Optional[float] = None
    profit_rate_pct: Optional[float] = None
    price_diff_fee: Optional[float] = None
    tax_fee: Optional[float] = None
    tax_rate_pct: Optional[float] = None
    quota_total_yuan: Optional[float] = None
    resource_lines: list[QuotaResourceLine] = Field(default_factory=list)


class RawBoqItem(BaseModel):
    """原始清单项契约（解析器插件统一输出此格式）"""

    seq_no: str = ""
    section_path: list[str] = Field(default_factory=list)
    is_section_header: bool = False
    boq_code: Optional[str] = None
    raw_name: str
    raw_features: str = ""
    raw_work_content: str = ""
    raw_unit: Optional[str] = None
    quantity: Optional[float] = None
    unit_price_yuan: Optional[float] = None
    total_price_yuan: Optional[float] = None
    breakdown: Optional[UnitPriceBreakdown] = None
    source_sheet: Optional[str] = None
    source_row: Optional[int] = None


class StandardizedBoqItem(BaseModel):
    """L1 标准清单项契约（SemanticStandardizerModule 输出）"""

    item_id: str
    project_id: str
    seq_no: str = ""
    domain: str
    category_code: str
    standard_name: str
    boq_code: Optional[str] = None
    section_path: list[str] = Field(default_factory=list)
    raw_text_full: str
    canonical_search_text: str
    standard_unit: Optional[str] = None
    raw_unit: Optional[str] = None
    unit_conversion_factor: float = 1.0
    normalized_quantity: Optional[float] = None
    normalized_unit_price_yuan: Optional[float] = None
    raw_unit_price_yuan: Optional[float] = None
    total_price_yuan: Optional[float] = None
    slots: dict[str, FeatureSlotValue] = Field(default_factory=dict)
    breakdown: Optional[UnitPriceBreakdown] = None
    quality_score: float = 1.0


class CanonicalProject(BaseModel):
    """跨模块传递的标准项目聚合根契约"""

    context: ProjectContext
    items: list[StandardizedBoqItem] = Field(default_factory=list)
    raw_items_count: int = 0
    header_items_count: int = 0
    quality_metrics: dict[str, float] = Field(default_factory=dict)
    audit_findings: list[Any] = Field(default_factory=list)
