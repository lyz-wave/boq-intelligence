from typing import Any, Protocol, runtime_checkable

from src.core.contracts.boq_item import CanonicalProject
from src.core.contracts.matching import AuditFinding, AuditSeverity


@runtime_checkable
class AuditRulePort(Protocol):
    """接缝 5：造价合规与合理性审查规则插件接口"""

    rule_id: str
    rule_name: str
    default_severity: AuditSeverity

    def check(self, project: CanonicalProject, historical_stats: dict[str, Any]) -> list[AuditFinding]:
        ...
