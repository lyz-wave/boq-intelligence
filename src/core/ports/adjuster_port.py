from typing import Protocol, runtime_checkable

from src.core.contracts.matching import AdjustedPriceBreakdown, MatchedCandidate
from src.core.contracts.project import ProjectContext


@runtime_checkable
class PriceAdjusterPort(Protocol):
    """接缝 4：跨区域跨期动态调价算子接口"""

    adjuster_id: str
    priority: int

    def supports(self, candidate: MatchedCandidate, target_ctx: ProjectContext) -> bool:
        ...

    def adjust(self, candidate: MatchedCandidate, target_ctx: ProjectContext) -> AdjustedPriceBreakdown:
        ...
