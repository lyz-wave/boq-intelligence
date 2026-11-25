from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field

from src.core.contracts.boq_item import StandardizedBoqItem
from src.core.contracts.project import ProjectContext


class ConfidenceLevel(str, Enum):
    HIGH = "high"      # >= 0.85 且无硬规格冲突（绿色）
    MEDIUM = "medium"  # 0.65 ~ 0.85（黄色）
    LOW = "low"        # < 0.65 或存在硬规格冲突（红色预警）


class MatchContext(BaseModel):
    """匹配请求上下文"""

    target_project_ctx: Optional[ProjectContext] = None
    preferred_provinces: list[str] = Field(default_factory=list)
    preferred_domains: list[str] = Field(default_factory=list)
    top_k: int = 5
    pipeline_name: str = "production_hybrid"
    auto_adjust_price: bool = True


class SlotComparison(BaseModel):
    """目标清单与历史候选清单在单个特征槽位上的逐项对比"""

    slot_key: str
    slot_label: str
    query_value: Any = None
    candidate_value: Any = None
    similarity: float = 0.0
    contribution_weight: float = 0.0
    is_hard_conflict: bool = False


class ExplainAttribution(BaseModel):
    """可解释性溯源与归因结果"""

    slot_comparisons: list[SlotComparison] = Field(default_factory=list)
    feature_contributions: dict[str, float] = Field(default_factory=dict)
    provenance_project_name: str = ""
    provenance_province: str = ""
    provenance_month: str = ""
    provenance_stage: str = ""
    provenance_quota_codes: list[str] = Field(default_factory=list)
    summary_reason: str = ""


class MatchedCandidate(BaseModel):
    """单条匹配候选结果"""

    candidate_item: StandardizedBoqItem
    candidate_project_ctx: ProjectContext
    raw_bm25_score: float = 0.0
    raw_dense_score: float = 0.0
    raw_slot_score: float = 0.0
    hard_spec_kernel_score: float = 1.0
    final_score: float
    confidence: ConfidenceLevel
    has_hard_spec_conflict: bool = False
    attribution: ExplainAttribution = Field(default_factory=ExplainAttribution)


class AdjustedPriceBreakdown(BaseModel):
    """动态调价结果契约"""

    adjuster_used: str
    original_unit_price_yuan: float
    adjusted_unit_price_yuan: float
    adjustment_factor: float = 1.0
    price_diff_details: list[str] = Field(default_factory=list)


class MatchResult(BaseModel):
    """PriceMatchingModule 的唯一输出契约"""

    query_item_id: str
    recommended_unit_price_yuan: Optional[float] = None
    confidence: ConfidenceLevel = ConfidenceLevel.LOW
    candidates: list[MatchedCandidate] = Field(default_factory=list)
    adjusted_price: Optional[AdjustedPriceBreakdown] = None
    warning_messages: list[str] = Field(default_factory=list)


class AuditSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    NOTICE = "notice"


class AuditFinding(BaseModel):
    """合规与合理性审查发现项契约"""

    rule_id: str
    rule_name: str
    severity: AuditSeverity
    item_id: Optional[str] = None
    seq_no: Optional[str] = None
    message: str
    suggestion: Optional[str] = None


class ExportOptions(BaseModel):
    """报表导出配置"""

    currency_unit: str = "元"
    include_formula: bool = True
    include_audit_sheet: bool = True
    include_provenance_sheet: bool = True
    include_summary_sheet: bool = True
    include_measure_sheet: bool = True
    measure_rate_pct: float = 4.5
    other_project_rate_pct: float = 11.5
    contingency_rate_pct: float = 5.0
    vat_rate_pct: float = 9.0
