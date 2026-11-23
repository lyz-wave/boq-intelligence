from pathlib import Path
from typing import Protocol, runtime_checkable

from src.core.contracts.boq_item import RawBoqItem
from src.core.contracts.project import PartialContext, ProjectFileBundle


@runtime_checkable
class DocumentParserPort(Protocol):
    """接缝 1：文档摄入解析插件接口"""

    parser_id: str
    priority: int

    def can_handle(self, file_path: Path, header_preview: list[str]) -> bool:
        """通过扩展名与表头指纹判断是否由本插件解析"""
        ...

    def parse(
        self, file_path: Path, bundle_ctx: ProjectFileBundle
    ) -> tuple[PartialContext, list[RawBoqItem]]:
        """解析文档返回局部项目上下文与原始清单行"""
        ...
