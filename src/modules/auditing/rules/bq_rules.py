from collections import defaultdict
from statistics import mean
from typing import Any

import yaml

from src.core.contracts.boq_item import CanonicalProject
from src.core.contracts.matching import AuditFinding, AuditSeverity
from src.core.contracts.project import CostStage
from src.core.paths import get_bundle_root
from src.core.registry import PluginRegistry


@PluginRegistry.register("audit_rules")
class BQ001MissingNameRule:
    rule_id = "BQ001"
    rule_name = "清单名称缺失检查"
    default_severity = AuditSeverity.ERROR

    def check(self, project: CanonicalProject, historical_stats: dict[str, Any]) -> list[AuditFinding]:
        findings: list[AuditFinding] = []
        for it in project.items:
            if not it.standard_name or it.standard_name == "未命名清单项":
                findings.append(
                    AuditFinding(
                        rule_id=self.rule_id,
                        rule_name=self.rule_name,
                        severity=self.default_severity,
                        item_id=it.item_id,
                        seq_no=it.seq_no,
                        message=f"清单行 [{it.seq_no}] 缺少有效项目名称",
                        suggestion="补充标准项目名称后重新执行标准化",
                    )
                )
        return findings


@PluginRegistry.register("audit_rules")
class BQ002UnitValidityRule:
    rule_id = "BQ002"
    rule_name = "计量单位缺失或非标检查"
    default_severity = AuditSeverity.ERROR

    def check(self, project: CanonicalProject, historical_stats: dict[str, Any]) -> list[AuditFinding]:
        findings: list[AuditFinding] = []
        for it in project.items:
            if not it.standard_unit:
                findings.append(
                    AuditFinding(
                        rule_id=self.rule_id,
                        rule_name=self.rule_name,
                        severity=self.default_severity,
                        item_id=it.item_id,
                        seq_no=it.seq_no,
                        message=f"清单 [{it.seq_no} {it.standard_name}] 缺失计量单位",
                        suggestion="请填写 m、m²、m³、t、个 等可计量物理单位",
                    )
                )
            elif it.standard_unit in {"批", "宗"}:
                findings.append(
                    AuditFinding(
                        rule_id=self.rule_id,
                        rule_name=self.rule_name,
                        severity=AuditSeverity.WARNING,
                        item_id=it.item_id,
                        seq_no=it.seq_no,
                        message=f"清单 [{it.seq_no} {it.standard_name}] 使用模糊单位 '{it.standard_unit}'",
                        suggestion="分部分项清单建议使用物理计量单位以便历史价格比对",
                    )
                )
        return findings


@PluginRegistry.register("audit_rules")
class BQ003CodeNormRule:
    rule_id = "BQ003"
    rule_name = "项目编码规范性检查"
    default_severity = AuditSeverity.WARNING

    def check(self, project: CanonicalProject, historical_stats: dict[str, Any]) -> list[AuditFinding]:
        findings: list[AuditFinding] = []
        if project.context.stage not in {CostStage.BID_CONTROL, CostStage.BID_QUOTE}:
            return findings
        for it in project.items:
            if not it.boq_code or len(it.boq_code) != 12 or not it.boq_code.isdigit():
                findings.append(
                    AuditFinding(
                        rule_id=self.rule_id,
                        rule_name=self.rule_name,
                        severity=self.default_severity,
                        item_id=it.item_id,
                        seq_no=it.seq_no,
                        message=f"清单 [{it.seq_no} {it.standard_name}] 在招标阶段编码 '{it.boq_code or '空'}' 不符合12位国标码",
                        suggestion="按 GB50500 补全 12 位数字清单编码",
                    )
                )
        return findings


@PluginRegistry.register("audit_rules")
class BQ004MissingHardSlotRule:
    rule_id = "BQ004"
    rule_name = "硬约束特征槽位缺失检查"
    default_severity = AuditSeverity.WARNING

    def __init__(self) -> None:
        self._categories_by_code: dict[str, dict[str, Any]] = {}
        self._all_categories: list[dict[str, Any]] = []
        self._load_domain_ontologies()

    def _load_domain_ontologies(self) -> None:
        domains_dir = get_bundle_root() / "configs" / "domains"
        if not domains_dir.exists():
            return
        for yfile in sorted(domains_dir.glob("*.yaml")):
            with open(yfile, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            dom_code = data.get("domain_code", "")
            for cat in data.get("categories", []):
                entry = {**cat, "domain_code": dom_code}
                self._categories_by_code[cat["category_code"]] = entry
                self._all_categories.append(entry)

    def _resolve_category(self, it: Any) -> dict[str, Any] | None:
        cat = self._categories_by_code.get(it.category_code)
        if cat:
            return cat
        txt = (it.raw_text_full or "").lower()
        best_cat = None
        best_score = 0.0
        for cand in self._all_categories:
            if it.domain and cand.get("domain_code") != it.domain:
                continue
            sc = sum(len(kw) for kw in cand.get("trigger_keywords", []) if kw.lower() in txt)
            if sc > best_score:
                best_score = sc
                best_cat = cand
        return best_cat

    def check(self, project: CanonicalProject, historical_stats: dict[str, Any]) -> list[AuditFinding]:
        findings: list[AuditFinding] = []
        for it in project.items:
            cat = self._resolve_category(it)
            if not cat:
                continue
            txt = it.raw_text_full or ""
            missing_labels: list[str] = []
            for slot_cfg in cat.get("slots", []):
                if not slot_cfg.get("hard_constraint", False):
                    continue
                if slot_cfg.get("audit_required") is False:
                    continue
                skip_kws = slot_cfg.get("audit_skip_keywords") or []
                if any(sk in txt for sk in skip_kws):
                    continue
                when_kws = slot_cfg.get("audit_when_keywords") or []
                if when_kws and not any(wk in txt for wk in when_kws):
                    continue
                if slot_cfg["key"] not in it.slots:
                    missing_labels.append(slot_cfg.get("label") or slot_cfg["key"])
            if missing_labels:
                findings.append(
                    AuditFinding(
                        rule_id=self.rule_id,
                        rule_name=self.rule_name,
                        severity=self.default_severity,
                        item_id=it.item_id,
                        seq_no=it.seq_no,
                        message=(
                            f"清单 [{it.seq_no} {it.standard_name}] 缺失领域本体必填硬约束特征: "
                            f"{'、'.join(missing_labels)}"
                        ),
                        suggestion=f"请补充 {' / '.join(missing_labels)} 等关键参数，避免跨规格误匹配",
                    )
                )
        return findings


@PluginRegistry.register("audit_rules")
class BQ005NegativeNumericRule:
    rule_id = "BQ005"
    rule_name = "数值格式与非正数检查"
    default_severity = AuditSeverity.ERROR

    def check(self, project: CanonicalProject, historical_stats: dict[str, Any]) -> list[AuditFinding]:
        findings: list[AuditFinding] = []
        for it in project.items:
            if it.normalized_quantity is not None and it.normalized_quantity < 0:
                findings.append(
                    AuditFinding(
                        rule_id=self.rule_id,
                        rule_name=self.rule_name,
                        severity=self.default_severity,
                        item_id=it.item_id,
                        seq_no=it.seq_no,
                        message=f"清单 [{it.seq_no} {it.standard_name}] 工程数量为负值 ({it.normalized_quantity})",
                        suggestion="核对原始提资数量",
                    )
                )
            if it.normalized_unit_price_yuan is not None and it.normalized_unit_price_yuan < 0:
                findings.append(
                    AuditFinding(
                        rule_id=self.rule_id,
                        rule_name=self.rule_name,
                        severity=self.default_severity,
                        item_id=it.item_id,
                        seq_no=it.seq_no,
                        message=f"清单 [{it.seq_no} {it.standard_name}] 综合单价为负值 ({it.normalized_unit_price_yuan})",
                        suggestion="核对单价列数据",
                    )
                )
        return findings


@PluginRegistry.register("audit_rules")
class BQ006ArithmeticConsistencyRule:
    rule_id = "BQ006"
    rule_name = "算术闭合一致性检查"
    default_severity = AuditSeverity.ERROR

    def check(self, project: CanonicalProject, historical_stats: dict[str, Any]) -> list[AuditFinding]:
        findings: list[AuditFinding] = []
        for it in project.items:
            if (
                it.normalized_quantity is not None
                and it.normalized_unit_price_yuan is not None
                and it.total_price_yuan is not None
            ):
                expected = it.normalized_quantity * it.normalized_unit_price_yuan
                tol = max(abs(expected) * 0.015, 50.0)
                if abs(expected - it.total_price_yuan) > tol:
                    findings.append(
                        AuditFinding(
                            rule_id=self.rule_id,
                            rule_name=self.rule_name,
                            severity=self.default_severity,
                            item_id=it.item_id,
                            seq_no=it.seq_no,
                            message=(
                                f"清单 [{it.seq_no} {it.standard_name}] 数量×单价={expected:.2f}元 "
                                f"与合价={it.total_price_yuan:.2f}元 不一致"
                            ),
                            suggestion="检查数量、单价或元/万元换算口径",
                        )
                    )
        return findings


@PluginRegistry.register("audit_rules")
class BQ007SameItemUnitConflictRule:
    rule_id = "BQ007"
    rule_name = "同表同名同特征单位冲突"
    default_severity = AuditSeverity.ERROR

    def check(self, project: CanonicalProject, historical_stats: dict[str, Any]) -> list[AuditFinding]:
        units_by_sig: dict[str, set[str]] = defaultdict(set)
        items_by_sig: dict[str, list] = defaultdict(list)
        for it in project.items:
            sig = it.raw_text_full.strip()
            if it.standard_unit:
                units_by_sig[sig].add(it.standard_unit)
                items_by_sig[sig].append(it)
        findings: list[AuditFinding] = []
        for sig, unit_set in units_by_sig.items():
            if len(unit_set) > 1:
                sample = items_by_sig[sig][0]
                findings.append(
                    AuditFinding(
                        rule_id=self.rule_id,
                        rule_name=self.rule_name,
                        severity=self.default_severity,
                        item_id=sample.item_id,
                        seq_no=sample.seq_no,
                        message=f"同一特征描述 [{sample.standard_name}] 在本表使用了多个不同单位: {sorted(unit_set)}",
                        suggestion="统一相同特征清单项的计量单位",
                    )
                )
        return findings


@PluginRegistry.register("audit_rules")
class BQ008SameItemPriceConflictRule:
    rule_id = "BQ008"
    rule_name = "同表同名同特征单价不一致"
    default_severity = AuditSeverity.WARNING

    def check(self, project: CanonicalProject, historical_stats: dict[str, Any]) -> list[AuditFinding]:
        prices_by_sig: dict[tuple[str, str], list] = defaultdict(list)
        for it in project.items:
            if it.normalized_unit_price_yuan is not None and it.standard_unit:
                prices_by_sig[(it.raw_text_full.strip(), it.standard_unit)].append(it)
        findings: list[AuditFinding] = []
        for (sig, unit), group in prices_by_sig.items():
            distinct = {round(x.normalized_unit_price_yuan or 0.0, 2) for x in group}
            if len(distinct) > 1:
                sample = group[0]
                findings.append(
                    AuditFinding(
                        rule_id=self.rule_id,
                        rule_name=self.rule_name,
                        severity=self.default_severity,
                        item_id=sample.item_id,
                        seq_no=sample.seq_no,
                        message=f"完全相同的清单 [{sample.standard_name} ({unit})] 出现多个单价: {sorted(distinct)}",
                        suggestion="核实不同工区或部位是否存在工艺差异，否则应拉齐单价",
                    )
                )
        return findings


@PluginRegistry.register("audit_rules")
class BQ009HistoricalOutlierRule:
    rule_id = "BQ009"
    rule_name = "历史同类单价大幅偏离预警"
    default_severity = AuditSeverity.WARNING

    def check(self, project: CanonicalProject, historical_stats: dict[str, Any]) -> list[AuditFinding]:
        avg_map: dict[tuple[str, str], list[float]] = historical_stats.get("avg_prices_by_name_unit", {})
        if not avg_map:
            return []
        findings: list[AuditFinding] = []
        for it in project.items:
            if it.normalized_unit_price_yuan is None or not it.standard_unit:
                continue
            hist_vals = avg_map.get((it.standard_name, it.standard_unit))
            if not hist_vals or len(hist_vals) < 2:
                continue
            avg_v = mean(hist_vals)
            if avg_v > 0:
                dev = abs(it.normalized_unit_price_yuan - avg_v) / avg_v
                if dev > 0.25:
                    findings.append(
                        AuditFinding(
                            rule_id=self.rule_id,
                            rule_name=self.rule_name,
                            severity=self.default_severity,
                            item_id=it.item_id,
                            seq_no=it.seq_no,
                            message=(
                                f"清单 [{it.seq_no} {it.standard_name}] 单价 {it.normalized_unit_price_yuan:.2f}元 "
                                f"偏离同类均值 {avg_v:.2f}元 达 {dev*100:.1f}%"
                            ),
                            suggestion="检查规格差异或核实是否需要主材调差",
                        )
                    )
        return findings


@PluginRegistry.register("audit_rules")
class BQ010LowConfidenceBlockRule:
    rule_id = "BQ010"
    rule_name = "置信度红灯未确认阻断"
    default_severity = AuditSeverity.ERROR

    def check(self, project: CanonicalProject, historical_stats: dict[str, Any]) -> list[AuditFinding]:
        low_ids: set[str] = set(historical_stats.get("low_confidence_item_ids", []))
        findings: list[AuditFinding] = []
        for it in project.items:
            if it.item_id in low_ids:
                findings.append(
                    AuditFinding(
                        rule_id=self.rule_id,
                        rule_name=self.rule_name,
                        severity=self.default_severity,
                        item_id=it.item_id,
                        seq_no=it.seq_no,
                        message=f"清单 [{it.seq_no} {it.standard_name}] 属于低置信度或硬规格冲突匹配项",
                        suggestion="请人工选择备选单价或手工审定后解除红灯预警",
                    )
                )
        return findings


@PluginRegistry.register("audit_rules")
class BQ011TextCleanlinessRule:
    rule_id = "BQ011"
    rule_name = "文本冗余与符号污染检查"
    default_severity = AuditSeverity.NOTICE

    def check(self, project: CanonicalProject, historical_stats: dict[str, Any]) -> list[AuditFinding]:
        findings: list[AuditFinding] = []
        for it in project.items:
            if "\n" in it.raw_text_full or "*" in it.raw_text_full:
                findings.append(
                    AuditFinding(
                        rule_id=self.rule_id,
                        rule_name=self.rule_name,
                        severity=self.default_severity,
                        item_id=it.item_id,
                        seq_no=it.seq_no,
                        message=f"清单 [{it.seq_no} {it.standard_name}] 存在未清洗的换行符或混用乘号 '*'",
                        suggestion="标准化引擎已做归一化，建议同步修正源提资表格式",
                    )
                )
        return findings


@PluginRegistry.register("audit_rules")
class BQ012DuplicateWorkContentRule:
    rule_id = "BQ012"
    rule_name = "关联工作内容重复计价风险"
    default_severity = AuditSeverity.NOTICE

    def check(self, project: CanonicalProject, historical_stats: dict[str, Any]) -> list[AuditFinding]:
        has_cable_with_head = any(
            ("电缆头制作" in it.raw_text_full) and ("电力电缆" in it.standard_name) for it in project.items
        )
        separate_heads = [it for it in project.items if "电力电缆头" in it.standard_name]
        if has_cable_with_head and separate_heads:
            return [
                AuditFinding(
                    rule_id=self.rule_id,
                    rule_name=self.rule_name,
                    severity=self.default_severity,
                    item_id=separate_heads[0].item_id,
                    seq_no=separate_heads[0].seq_no,
                    message=(
                        f"电力电缆清单工作内容已包含'电缆头制作及安装'，同时本表另有 {len(separate_heads)} 项独立"
                        f"列项的'电力电缆头'，请核查主材与安装费是否重复计取"
                    ),
                    suggestion="核对终端头单价是否仅含材料/辅材或已作扣减",
                )
            ]
        return []


@PluginRegistry.register("audit_rules")
class BQ013CompanionProcessOmissionRule:
    rule_id = "BQ013"
    rule_name = "关联工序与配套子目漏项检查"
    default_severity = AuditSeverity.WARNING

    _DEFAULT_RULES: list[dict[str, Any]] = [
        {
            "rule_key": "buried_pipe_earthwork",
            "description": "室外埋地给排水管道缺少沟槽土石方开挖/回填配套子目",
            "severity": "warning",
            "trigger_keywords": ["PE管", "HDPE", "双壁波纹管", "球墨铸铁管", "给水管", "排水管", "污水管", "混凝土涵管"],
            "exclude_item_keywords": ["室内", "架空", "明敷", "井下", "三通", "阀门"],
            "min_total_qty": 20.0,
            "companion_keywords": ["沟槽", "管沟", "土方开挖", "土（石）方开挖", "挖土", "土方回填", "土（石）方回填"],
            "suggestion": "若为室外埋地敷设，建议核对是否遗漏沟槽土方开挖、基础垫层及回填子目",
        },
        {
            "rule_key": "power_cable_termination",
            "description": "电力电缆敷设缺少电缆终端头/铜鼻子配套子目",
            "severity": "notice",
            "trigger_keywords": ["电力电缆"],
            "exclude_item_keywords": ["电缆头", "终端", "含电缆头", "电缆头制作"],
            "min_total_qty": 30.0,
            "companion_keywords": ["电缆头", "终端", "铜鼻子"],
            "suggestion": "长距离电力电缆敷设通常需配套列计热缩/冷缩电缆终端头及铜接线端子，除非工作内容已明确含头",
        },
    ]

    def __init__(self) -> None:
        cfg_path = get_bundle_root() / "configs" / "companion_rules.yaml"
        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            self._companion_rules = data.get("companion_rules") or self._DEFAULT_RULES
        else:
            self._companion_rules = self._DEFAULT_RULES

    def check(self, project: CanonicalProject, historical_stats: dict[str, Any]) -> list[AuditFinding]:
        if len(project.items) == 0:
            return []
        all_texts = [f"{it.standard_name} {it.raw_text_full}" for it in project.items]
        findings: list[AuditFinding] = []

        for rule in self._companion_rules:
            trig_kws = rule.get("trigger_keywords", [])
            excl_kws = rule.get("exclude_item_keywords", [])
            comp_kws = rule.get("companion_keywords", [])
            min_qty = float(rule.get("min_total_qty", 0.0))

            triggered_items = []
            for it, txt in zip(project.items, all_texts):
                if any(tk in txt for tk in trig_kws) and not any(ek in txt for ek in excl_kws):
                    triggered_items.append(it)

            if not triggered_items:
                continue
            total_qty = sum(float(it.normalized_quantity or 0.0) for it in triggered_items)
            if total_qty < min_qty:
                continue

            has_companion = any(any(ck in txt for ck in comp_kws) for txt in all_texts)
            if not has_companion:
                first = triggered_items[0]
                sev_str = str(rule.get("severity", "warning")).lower()
                sev = (
                    AuditSeverity.ERROR
                    if sev_str == "error"
                    else (AuditSeverity.NOTICE if sev_str == "notice" else AuditSeverity.WARNING)
                )
                findings.append(
                    AuditFinding(
                        rule_id=self.rule_id,
                        rule_name=self.rule_name,
                        severity=sev,
                        item_id=first.item_id,
                        seq_no=first.seq_no,
                        message=(
                            f"{rule['description']}（共涉及 {len(triggered_items)} 项，合计工程量 {total_qty:g} "
                            f"{first.standard_unit or ''}）"
                        ),
                        suggestion=rule.get("suggestion"),
                    )
                )
        return findings
