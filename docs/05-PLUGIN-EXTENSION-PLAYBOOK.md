# 可插拔扩展与后期维护开发手册（Plugin Extension Playbook）

> **关联文档**：[架构契约与接缝规范](01-ARCHITECTURE-AND-CONTRACTS.md) ｜ [ADR-0001 微内核与深模块架构](adr/0001-microkernel-deep-modules-and-plugin-seams.md)

本手册面向后期系统维护人员、二次开发工程师及科研算法同学，提供基于本系统**“微内核 + 深模块 + 端口与适配器”**架构的五类高频扩展实操食谱。

> **核心扩展铁律**：新增功能时，优先在对应的 `adapters/`、`rules/` 或 `configs/domains/` 目录**新增独立文件并注册**，严禁修改 `src/core/` 和其他深模块的已有业务逻辑。

---

## 食谱 1：如何新增一种造价表格解析插件（`DocumentParserPort`）

**适用场景**：业务部门新接入一种从未处理过的造价格式（例如广联达导出 `.xml`、某省水利厅新版 `.xlsx` 导表格式或斯维尔报表）。

### 步骤 1：在 `src/modules/ingestion/adapters/` 下新建插件文件
例如新建 `glodon_export_adapter.py`，实现 `DocumentParserPort` 协议的 `can_handle` 和 `parse` 两个方法，并使用 `@PluginRegistry.register("parsers")` 挂载：

```python
from pathlib import Path
from src.core.contracts.boq_item import RawBoqItem
from src.core.contracts.project import ProjectFileBundle
from src.core.ports.parser_port import DocumentParserPort, PartialContext
from src.core.registry import PluginRegistry


@PluginRegistry.register("parsers")
class GlodonExportParser:
    parser_id: str = "glodon_standard_excel"

    def can_handle(self, file_path: Path, header_preview: list[str]) -> bool:
        # 1. 通过扩展名与表头关键词指纹判断是否命中
        if file_path.suffix.lower() not in {".xls", ".xlsx"}:
            return False
        joined = " ".join(header_preview)
        return "分部分项工程和单价措施项目清单" in joined and "项目编码" in joined

    def parse(
        self, file_path: Path, bundle_ctx: ProjectFileBundle
    ) -> tuple[PartialContext, list[RawBoqItem]]:
        # 2. 内部实现表格读取、空列清洗与树状层级恢复（复杂度完全收敛在此类内）
        partial_ctx = PartialContext(source_file=file_path.name)
        raw_items: list[RawBoqItem] = []
        # ... 解析各行填充 RawBoqItem(seq_no=..., section_path=..., raw_name=..., ...)
        return partial_ctx, raw_items
```

### 步骤 2：编写独立契约测试验证
在 `tests/modules/test_ingestion_glodon.py` 中直接调用外层门面接口 `IngestionModule().ingest(bundle)`，验证输出 `CanonicalProject` 中的清单条数与合价总额闭合即可。**无需改动任何入库、标准化、检索与前端代码。**

---

## 食谱 2：如何零代码新增一个工程专业领域（YAML 本体扩展）

**适用场景**：系统前期已落地水利生态（`01`）、土地整治（`05`）、通用安装（`07`），后期需要快速拓展到**煤矿疏干水工程（`04_coal_mine_water`）**或**海洋生态修复工程（`06_marine_ecology`）**。

### 步骤 1：在 `configs/domains/` 下新增 YAML 文件
例如新建 `configs/domains/04_coal_mine_water.yaml`：

```yaml
domain_code: "04_coal_mine_water"
domain_name: "煤矿疏干水工程"

unit_normalizations:
  "米": { standard: "m", factor: 1.0 }
  "立方米": { standard: "m³", factor: 1.0 }
  "台": { standard: "台", factor: 1.0 }

name_synonyms:
  "矿井排水管": "无缝钢管敷设"
  "井下防爆泵": "矿用防爆潜水泵"

categories:
  - category_code: "04_01_high_pressure_pipe"
    category_name: "高压疏干水输水管道"
    trigger_keywords: ["无缝钢管", "矿用涂塑钢管", "疏干水管", "DN"]
    slots:
      - key: "pipe_material"
        label: "管材及防腐"
        dimension: "entity_spec"
        hard_constraint: true
        weight: 0.28
        regex: "(无缝钢管|矿用涂塑复合钢管|球墨铸铁管|3PE防腐)"
      - key: "nominal_diameter_mm"
        label: "公称直径(mm)"
        dimension: "entity_spec"
        hard_constraint: true
        weight: 0.35
        regex: "(?:DN|管径|φ|Φ)\\s*(\\d+)"
      - key: "pressure_rating_mpa"
        label: "工作压力(MPa)"
        dimension: "entity_spec"
        hard_constraint: true
        weight: 0.22
        regex: "(\\d+(?:\\.\\d+)?)\\s*MPa"
      - key: "install_env"
        label: "井下/地面工况"
        dimension: "env_condition"
        hard_constraint: true
        weight: 0.15
        regex: "(井下|巷道|地面|直埋|架空)"
```

### 步骤 2：验收生效
`SemanticStandardizerModule` 与 `PriceMatchingModule` 会自动扫描加载该 YAML 文件：
* 新导入属于该领域的历史项目自动抽取上述 4 个特征槽位；
* 重排引擎自动读取对应的 `weight`（`0.28 + 0.35 + 0.22 + 0.15 = 1.00`）和 `hard_constraint` 标记计算衰减核；
* 合规检查规则 `BQ004` 自动检查新清单是否漏填 `公称直径` 或 `工作压力`。

---

## 食谱 3：如何新增一个匹配重排算法插件（`RerankerPort`）

**适用场景**：科研团队开发了新的深度学习模型（如 `LSTM + Attention`、`TransE/R-GCN` 图神经网络或微调后的 Cross-Encoder 模型），需要挂载到线上替换默认引擎或在评测台运行消融对比实验。

### 步骤 1：在 `src/modules/matching/rerankers/` 下新增重排适配器

```python
from src.core.contracts.boq_item import StandardizedBoqItem
from src.core.contracts.matching import ConfidenceLevel, MatchContext, MatchedCandidate
from src.core.contracts.project import ProjectContext
from src.core.registry import PluginRegistry


@PluginRegistry.register("rerankers")
class LstmCosineReranker:
    reranker_id: str = "lstm_cosine_baseline"

    def __init__(self, weights_path: str = "models/lstm_boq.pt"):
        self.weights_path = weights_path

    def score(
        self,
        query: StandardizedBoqItem,
        candidates: list[tuple[StandardizedBoqItem, ProjectContext]],
        ctx: MatchContext,
    ) -> list[MatchedCandidate]:
        results: list[MatchedCandidate] = []
        # 使用 self._encode_lstm(text) 计算序列向量与余弦相似度，封装 MatchedCandidate 返回
        return sorted(results, key=lambda x: x.final_score, reverse=True)
```

### 步骤 2：在 `configs/pipelines.yaml` 中声明挂载

```yaml
matching_pipelines:
  production_hybrid:
    retrievers: ["bm25", "bge_dense", "slot_exact"]
    reranker: "hybrid_hard_spec_kernel"
    explainer: "shap_ahp_explainer"

  academic_lstm_baseline:
    retrievers: ["bm25", "bge_dense"]
    reranker: "lstm_cosine_baseline"
    explainer: "shap_ahp_explainer"
```

调用 `PriceMatchingModule.match(query, MatchContext(pipeline_name="academic_lstm_baseline"))` 即可一键切换对比，线上生产流水线 `production_hybrid` 完全不受影响。

---

## 食谱 4：如何新增一条动态调价算子（`PriceAdjusterPort`）

**适用场景**：后续需要支持“按地区人工费单价比例缩放”或“针对特定设备按功率线性插值”等新调价逻辑。

```python
from src.core.contracts.matching import AdjustedPriceBreakdown, MatchedCandidate
from src.core.contracts.project import ProjectContext
from src.core.registry import PluginRegistry


@PluginRegistry.register("adjusters")
class LaborRatioAdjuster:
    adjuster_id: str = "labor_ratio_adjuster"

    def supports(self, candidate: MatchedCandidate, target_ctx: ProjectContext) -> bool:
        return bool(
            candidate.candidate_item.breakdown
            and candidate.candidate_project_ctx.labor_rates
            and target_ctx.labor_rates
        )

    def adjust(
        self, candidate: MatchedCandidate, target_ctx: ProjectContext
    ) -> AdjustedPriceBreakdown:
        # 计算人工单价差异并输出 AdjustedPriceBreakdown
        ...
```

---

## 食谱 5：如何新增一条造价合规审查规则（`AuditRulePort`）

**适用场景**：总工审价新增一条企业内部内控红线（例如 `BQ013`：检测高值主材暂估价比例是否超过分部分项费的 15%）。

在 `src/modules/auditing/rules/` 下新建 `bq013_provisional_ratio.py`：

```python
from src.core.contracts.boq_item import CanonicalProject
from src.core.contracts.matching import AuditFinding, AuditSeverity
from src.core.registry import PluginRegistry


@PluginRegistry.register("audit_rules")
class ProvisionalPriceRatioRule:
    rule_id: str = "BQ013"
    rule_name: str = "暂估价占比超限审查"
    default_severity: AuditSeverity = AuditSeverity.WARNING

    def check(self, project: CanonicalProject, historical_stats: dict) -> list[AuditFinding]:
        findings: list[AuditFinding] = []
        # 检查 project.items 逻辑，触发时 append(AuditFinding(...))
        return findings
```

保存文件后，系统启动扫描 `audit_rules` 注册池时会自动加载并执行 `BQ013`，前端审查面板与导出 Excel 附表自动渲染该规则结果。

---

## 六、 深模块契约测试（Contract Testing）规范

为避免单元测试与内部实现细节强耦合导致“一改代码测试全红”，所有测试必须严格遵循以下准则：

1. **接口即测试面（The Interface is the Test Surface）**：测试只允许通过六大深模块的公共门面入口（如 `IngestionModule.ingest()`、`PriceMatchingModule.match()`）调用并断言返回契约，禁止跨模块直接 Mock 或断言内部私有辅助函数。
2. **用真实内存适配器替代重量级基础设施**：测试数据库仓储与向量存储时，注入实现同一仓储协议的 `InMemoryRepository` 或临时内存 `sqlite:///:memory:`，实现百毫秒级全量回归测试。
3. **必备四大金标准回归测试用例**：
   * **西柳沟导表金额量纲测试**：验证 `3.41 × 13823.75 ≈ 47100 元`（原始 Excel `4.71 万元` 必须准确转换为 `47100.0 元`）；
   * **高明区跨表关联测试**：验证 `1.1.1.2.2 土（石）方运输，运距5km` 成功关联 L2 定额 `10224` 及柴油消耗 `127.56 kg`；
   * **二级泵站 PDF 规格抗干扰测试**：查询 `ZR-YJV-0.6/1KV-3×150+1×95` 时，Top-1 绝对禁止返回 `3×25+1×16`；
   * **审计规则隔离测试**：构造单位不一致、算术乘积错误、硬规格缺失清单，验证 `BQ002`、`BQ004`、`BQ006`、`BQ007` 精准命中且自动跳过章节小计行。
