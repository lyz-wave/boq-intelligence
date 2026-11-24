from pathlib import Path
from typing import Any, Optional

import yaml

from src.core.contracts.boq_item import CanonicalProject
from src.core.contracts.matching import AuditFinding
from src.core.paths import get_bundle_root
from src.core.ports.audit_port import AuditRulePort
from src.core.registry import PluginRegistry
from src.modules.auditing import rules as _loaded_rules  # noqa: F401


class AuditLinterModule:
    """造价合规与合理性审查深模块：对外仅暴露 audit(project) -> list[AuditFinding]."""

    def __init__(
        self,
        rules: Optional[list[AuditRulePort]] = None,
        pipelines_cfg_path: Optional[Path] = None,
    ) -> None:
        all_rules = rules or PluginRegistry.instantiate_all("audit_rules")
        cfg_path = pipelines_cfg_path or get_bundle_root() / "configs" / "pipelines.yaml"
        enabled_ids: set[str] | None = None
        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            rule_list = cfg.get("auditing", {}).get("enabled_rules")
            if rule_list:
                enabled_ids = set(rule_list)
        self._rules = [r for r in all_rules if enabled_ids is None or r.rule_id in enabled_ids]

    def audit(
        self, project: CanonicalProject, historical_stats: Optional[dict[str, Any]] = None
    ) -> list[AuditFinding]:
        stats = historical_stats or {}
        findings: list[AuditFinding] = []
        for rule in self._rules:
            findings.extend(rule.check(project, stats))
        return findings
