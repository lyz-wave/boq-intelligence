# 造价历史数据治理、多源 ETL 与“规则库+价格库”构建规范

> **关联文档**：[统一领域词汇表 (GLOSSARY.md)](../GLOSSARY.md) ｜ [架构契约与接缝规范](01-ARCHITECTURE-AND-CONTRACTS.md) ｜ [ADR-0004 三级价格存储模型](adr/0004-three-tier-price-storage-for-dynamic-repricing.md)
> **科研对应交付物**：《造价历史数据治理与清洗报告》《工程量计算规则库》《工程价格数据库》

---

## 一、 历史造价数据异构性诊断（基于工作区样本实证）

通过对工作区 `data/` 目录下 3 套真实历史工程文件的底层解析审计，发现直接阻断造价数据资产化与 AI 训练的**五大典型异构与脏数据形态**：

| 脏数据形态 | 真实样本出处 | 具体表现与技术危害 |
| :--- | :--- | :--- |
| **1. 说明与计价表物理分离** | [西柳沟概算.docx](../data/西柳沟流域生态综合治理工程初步设计概算09.docx) + [单行本导表.xls](../data/西柳沟流域生态综合治理工程10.19单行本导表.xls)；<br/>[高明区预算.doc](../data/高明区明城镇全域土地综合整治项目（一标段.doc) + [第四批次.xls](../data/高明区明城镇全域土地综合整治项目第四批次.xls) | 计价 Excel 中完全不包含项目所在省区、价格基准年月、定额标准、人工单价及取费费率，这些关键 L0 上下文全部分散在 Word 编制说明的段落与嵌入表格中。孤立导入 Excel 会产生大量“无背景裸单价”。 |
| **2. 大量跨列合并与空列冗余** | [高明区第四批次.xls](../data/高明区明城镇全域土地综合整治项目第四批次.xls) | `工程量清单报价表` 共有 14 列，有效数据仅分布在 6 列（其余 8 列全为空值）；`工程量清单单价汇总表` 共 21 列、`清单单价分析表4` 共 16 列，大量字段由造价软件导出排版合并单元格造成，直接用 `pandas.read_excel` 会导致列名错位与 `Unnamed` 列泛滥。 |
| **3. 隐式树状父子层级结构** | 西柳沟 `1.1.1.1`、高明区 `1.1.1.2.2` | 章节汇总行（如 `1.1 土地平整工程`）与明细计价行混排在同一张表，章节行只有合价、无单价与数量；明细行（如 `1.1.1.2.2 土（石）方运输`）脱离父级行（`1.1.1.2 地表清理 0.05m厚`）就会丢失施工部位语义。 |
| **4. 跨 Sheet 多对一共享映射** | 高明区 `工程量清单报价表` 与 `清单单价分析表4`（2564 行） | 一张单价分析表被多个片区清单项共用，其 `单价编号` 列采用斜杠拼接格式（如 `1.1.1.1.2/1.1.1.2.2/1.3.1.1.3/...`）；且单价分析表的 `定额单位`（如 `100m³`、`100棵`）与报价表的 `计量单位`（`m³`、`棵`）存在 100 倍量纲差。 |
| **5. PDF 跨页断行与符号混乱** | [二级泵站电缆综合单价.pdf](../data/二级泵站电缆综合单价.pdf)（共 13 页） | 单元格折行导致核心规格被硬截断（如 `ZR-YJV-0.6/\n1KV-5×16`、`35K\nV-3×150`）；同一表内第 1~27 项特征写在多行文本中，而第 28~44 项特征直接并入“项目名称”列；规格乘号同时存在 `*` 与 `×`；甚至同份 PDF 内清单表叫 `ZR-YJV-0.6/1KV-3*150+1*95`，末尾主材表却写作 `R-YJV-0.6/1KV-3*150+1*95`。 |

---

## 二、 六步标准化 ETL 数据清洗流水线规范

文档摄入模块（`IngestionModule`）与语义标准化模块（`SemanticStandardizerModule`）协同执行以下六步自动化清洗算子：

```mermaid
flowchart LR
    S1["Step 1：表头指纹嗅探与插件路由"] --> S2["Step 2：合并列压缩与PDF跨行修复"]
    S2 --> S3["Step 3：栈式树状层级路径恢复"]
    S3 --> S4["Step 4：跨Sheet主键拆分与L2分析关联"]
    S4 --> S5["Step 5：量纲(万元/100m³)与符号(*→×)归一化"]
    S5 --> S6["Step 6：Word L0上下文(省/定额/主材价)注入"]
```

1. **Step 1：表头指纹嗅探与插件路由**
   读取工作表前 10 行文本或 PDF 首行表格标题，匹配解析器插件签名（例如命中 `单价编号:` 与 `直接工程费` 则路由至单价分析表解析器；命中 `工程或费用名称` + `合计（万元）` 则路由至水利概算导表插件）。
2. **Step 2：空列压缩与 PDF 跨行词修复**
   剔除全空列并重建紧凑列索引；对 PDF 单元格换行符执行**工程符号粘连规则**：当换行符前后紧邻英文字母、数字、小数点、斜杠或单位符号（如 `0.6/\n1KV`、`35K\nV`、`3*2\n.5`）时直接无缝拼接，否则替换为空格。
3. **Step 3：树状父子层级栈（Breadcrumb Stack）恢复**
   根据序号层级深度（`.` 分隔段数 `1` → `1.1` → `1.1.1` → `1.1.1.1`）以及“存在合价但单位/数量/单价均为空”的章节特征，维护单调层级栈，为每一条叶子清单行自动挂载完整的 `section_path`，并将纯章节汇总行标记为 `is_section_header=True` 过滤出待匹配池（防止与 `KanadeK/boq-check` 同样出现章节行误判）。
4. **Step 4：跨 Sheet 多表联动与斜杠编号展开**
   解析 `清单单价分析表` 时，对 `单价编号` 字段按 `/` 切分拆解，并按定额单位换算系数将 L2 工料机消耗量折算至 L1 标准清单单位，完成“报价表 ↔ 单价汇总表 ↔ 单价分析表 ↔ 主材表”四表主键对齐。
5. **Step 5：金额与计量单位量纲归一化**
   * 识别表头 `合计（万元）`，自动乘以 `10000` 写入 `total_price_yuan`；
   * 校验 `|quantity × unit_price_yuan - total_price_yuan|` 算术闭合度；
   * 统一全半角括号及规格符号（`*` → `×`，`m2`/`㎡` → `m²`）。
6. **Step 6：Word 编制说明宏观上下文（L0）注入**
   解析项目包内的 `.doc`/`.docx` 文档，提取行政区划（省份/区县）、价格年月、定额文件文号列表、人工预算单价、电水风单价、取费费率表及主要材料预算价格表，注入 `ProjectContext`。

---

## 三、 五维数据质量评估指标体系

每次`项目包`清洗完成后，系统自动计算五维数据质量评分（总分 $[0, 1]$），低于 `0.75` 的批次进入人工复核队列，禁止直接污染核心价格库：

| 评估维度 | 定义与检查项 | 量化计算公式 | 准入阈值 |
| :--- | :--- | :--- | :---: |
| **完整性 (Completeness)** | L0 核心上下文（省/年月/领域）及 L1 清单必填列（名称、单位、单价）非空比例 | $Q_C = 1 - \frac{\text{核心字段缺失数}}{\text{总清单行} \times \text{核心字段数}}$ | $\ge 0.95$ |
| **准确性 (Accuracy)** | 数量 $\times$ 综合单价与合价算术闭合率（容忍四舍五入误差 $\le 1\%$） | $Q_A = \frac{\text{算术闭合清单行数}}{\text{含数量与合价总行数}}$ | $\ge 0.98$ |
| **一致性 (Consistency)** | 计量单位可识别归一化率、同表内同名同特征清单单价无冲突率 | $Q_S = \frac{\text{通过 } \text{BQ001}\sim\text{BQ012} \text{ 校验行数}}{\text{总清单行数}}$ | $\ge 0.92$ |
| **时效性 (Timeliness)** | 价格基准期距当前年份的衰减权重（用于入库打标与检索降权） | $Q_T = \exp\left(-\lambda \cdot \Delta\text{Years}\right),\ \lambda=0.08$ | 记录打标 |
| **唯一性 (Uniqueness)** | 同项目同分部路径下重复/冗余清单项去重后占比 | $Q_U = 1 - \frac{\text{重复清单行数}}{\text{总清单行数}}$ | $\ge 0.99$ |

---

## 四、 “价格库 + 规则库”双库物理 Schema 设计

### 4.1 工程价格数据库物理表结构（SQLite / PostgreSQL 兼容 DDL）

为兼顾单机离线便携（SQLite + JSON1）与企业服务器级高并发检索（PostgreSQL + JSONB + `pgvector`），数据库采用标准关系型 + JSON 结构化混合存储，映射 L0/L1/L2 三级模型：

```sql
-- 1. L0 项目宏观上下文主表
CREATE TABLE projects (
    project_id          VARCHAR(64) PRIMARY KEY,
    project_name        VARCHAR(255) NOT NULL,
    domain              VARCHAR(64)  NOT NULL,  -- 六大领域枚举码
    province            VARCHAR(64)  NOT NULL,
    city                VARCHAR(64),
    district            VARCHAR(64),
    stage               VARCHAR(64)  NOT NULL,  -- 投资估算 / 初步设计概算 / 施工图预算 ...
    price_base_month    VARCHAR(16)  NOT NULL,  -- YYYY-MM
    quota_standards     JSON         NOT NULL,  -- 适用定额文件列表
    labor_rates         JSON,                   -- 人工预算单价字典
    utility_rates       JSON,                   -- 电/风/水单价字典
    fee_rules           JSON,                   -- 措施/间接费/利润/税率规则表
    total_investment_yuan DECIMAL(18, 2),
    quality_score       DECIMAL(5, 4) DEFAULT 1.0,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. L1 标准清单与综合单价核心表
CREATE TABLE boq_items (
    item_id                 VARCHAR(64) PRIMARY KEY,
    project_id              VARCHAR(64) NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    seq_no                  VARCHAR(64),
    section_path            JSON        NOT NULL,  -- ["光明东片区", "土地平整工程", ...]
    domain                  VARCHAR(64) NOT NULL,
    category_code           VARCHAR(64) NOT NULL,
    standard_name           VARCHAR(255) NOT NULL,
    boq_code                VARCHAR(32),
    raw_text_full           TEXT        NOT NULL,
    canonical_search_text   TEXT        NOT NULL,  -- 供 BM25 与向量索引使用
    standard_unit           VARCHAR(32),
    unit_conversion_factor  DECIMAL(12, 6) DEFAULT 1.0,
    normalized_quantity     DECIMAL(18, 4),
    normalized_unit_price_yuan DECIMAL(18, 4),     -- 归一化元单价
    total_price_yuan        DECIMAL(18, 2),
    slots_json              JSON        NOT NULL,  -- 三维特征槽位 JSON（硬规格+软特征）
    embedding_vector        BLOB,                  -- BGE 稠密向量（PG 下替换为 vector(1024)）
    has_l2_breakdown        BOOLEAN DEFAULT FALSE,
    data_source             VARCHAR(32) DEFAULT 'historical_import' -- historical_import | review_feedback
);

CREATE INDEX idx_boq_domain_cat ON boq_items(domain, category_code);
CREATE INDEX idx_boq_std_name   ON boq_items(standard_name);

-- 3. L2 单价构成分析表（工料机与取费明细，可选增强）
CREATE TABLE boq_price_breakdowns (
    breakdown_id        VARCHAR(64) PRIMARY KEY,
    item_id             VARCHAR(64) NOT NULL REFERENCES boq_items(item_id) ON DELETE CASCADE,
    quota_codes         JSON        NOT NULL,      -- 如 ["10224"] 或 ["10347", "10360"]
    quota_unit          VARCHAR(32),
    direct_labor_fee    DECIMAL(18, 4),
    direct_material_fee DECIMAL(18, 4),
    direct_machinery_fee DECIMAL(18, 4),
    indirect_fee        DECIMAL(18, 4),
    profit_fee          DECIMAL(18, 4),
    tax_fee             DECIMAL(18, 4),
    resource_lines_json JSON        NOT NULL       -- 工料机消耗量数组（支持主材替换重算）
);

-- 4. 四维主要材料基准价格表（项目-材料-时间-区域）
CREATE TABLE material_prices (
    mat_price_id            VARCHAR(64) PRIMARY KEY,
    project_id              VARCHAR(64) REFERENCES projects(project_id) ON DELETE CASCADE,
    material_code           VARCHAR(64),
    standard_material_name  VARCHAR(255) NOT NULL,
    spec_model              VARCHAR(255),
    unit                    VARCHAR(32)  NOT NULL,
    base_price_yuan         DECIMAL(18, 4),
    budget_price_excl_tax   DECIMAL(18, 4),
    province                VARCHAR(64),
    district                VARCHAR(64),
    price_month             VARCHAR(16)
);

-- 5. 三级校审数据回流与增量学习日志表
CREATE TABLE review_feedback_logs (
    feedback_id         VARCHAR(64) PRIMARY KEY,
    target_project_id   VARCHAR(64) NOT NULL,
    query_text          TEXT        NOT NULL,
    matched_item_id     VARCHAR(64),
    ai_recommended_price DECIMAL(18, 4),
    final_approved_price DECIMAL(18, 4) NOT NULL,
    modify_reason       TEXT,                      -- 修改原因（如“运距增至8km”、“铜价调差”）
    reviewer_role       VARCHAR(32),               -- 编制人 / 校核人 / 审查人
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 4.2 工程量计算与计价规则知识图谱（Rule Knowledge Graph）

将分散在国家、部委及地方定额文件中的计算规则、取费办法与工程量清单关联关系抽象为**“规则节点—适用场景—定额/取费应用”**四元关联图谱（支持内存邻接图与 Neo4j 双引擎存储）：

* **实体节点类型（Node Labels）**：
  * `EngineeringDomain`（工程领域，如 `水利/水环境生态治理`、`全域土地综合整治`）
  * `QuotaStandard`（编制规定/定额体系，如 `水总〔2024〕323号`、`财综〔2021〕128号`、`粤国土资耕保发〔2018〕118号`）
  * `SectionCategory`（工程类别/分部，如 `土方工程`、`灌木栽植`、`通用安装工程`）
  * `BoqTemplate`（标准清单特征模板，如 `土石方运输-5km`、`电力电缆-室内敷设`）
  * `QuotaSubItem`（具体定额子目，如 `定额10224`、`定额10347+10360`）
  * `FeeRule`（取费规则节点，如 `间接入费率`、`措施费率`）
  * `MaterialCategory`（核心主材，如 `柴油`、`水泥42.5`、`片石`）

* **关系边类型（Edge Types）与真实工程三元组示例**：
  1. `(01_water_ecology) -[APPLIES_STANDARD {region: "全国/内蒙古", stage: "初步设计概算"}]-> (水总〔2024〕323号)`
  2. `(水总〔2024〕323号) -[HAS_FEE_RATE {category: "土方工程", base: "直接费", rate: 0.12}]-> (间接费规则)`
  3. `(水总〔2024〕323号) -[HAS_FEE_RATE {category: "砌筑工程", base: "直接费", rate: 0.06}]-> (间接费规则)`
  4. `(05_land_consolidation) -[APPLIES_STANDARD {region: "广东省佛山市四类区", labor_A: 107.1, labor_B: 76.7}]-> (粤国土资耕保发〔2018〕118号)`
  5. `(土（石）方运输，运距5km) -[MAPS_TO_QUOTA {unit: "100m³"}]-> (定额10224)`
  6. `(定额10224) -[REQUIRES_MATERIAL {qty: 127.56, unit: "kg/100m³"}]-> (柴油)`
  7. `(电力电缆-室内敷设) -[INCLUDES_WORK]-> (电缆敷设 + 电缆头制作及安装)`（用于在组价时防重防漏：若清单工作内容已包含电缆头制作安装，规则引擎提示审查是否存在第 28~44 项电缆终端头重复计列风险）。
