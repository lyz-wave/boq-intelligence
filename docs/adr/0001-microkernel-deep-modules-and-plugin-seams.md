---
status: accepted
date: 2026-11-24
---

# 0001. 采用“微内核标准数据契约 + 六大深模块 + 插件注册表”架构

为了在满足一线技经人员日常快速搜价、批量组价需求的同时，支持后续不断接入新的造价软件导出格式、扩展六大工程专业领域，并支撑科研团队插拔不同深度学习算法进行对比实验，系统采用**微内核（Canonical Data Contracts）+ 六大深模块（Deep Modules：Ingestion, Standardizer, Matching, Pricing, Auditing, Exporter）+ 端口与适配器注册表（Ports & Adapters Registry）**架构。

各深模块仅暴露单一的高内聚门面接口（Facade Interface），模块间严禁直接传递原始 Excel/PDF 行对象或数据库 ORM 实例，仅通过 `src/core/contracts/` 中定义的不可变 Pydantic 标准数据契约通信；所有高频变化点（文档解析器、特征抽取器、召回与重排算法、动态调价算子、合规审查规则）均通过 Python `Protocol` 接缝（Seam）定义为独立可插拔的适配器（Adapter）。

## Considered Options

- **方案 A：传统三层 MVC 架构（Controller → Service → DAO）**
  - *放弃原因*：文件解析逻辑、特征提取正则、数据库查询与匹配打分极易在 Service 层中缠绕耦合；后期每增加一种非标 Excel 格式（如西柳沟水利概算单行本 vs 高明区土地整治 6 表联动）或切换一种匹配算法，都必须侵入修改核心业务代码，回归测试成本极高。
- **方案 B：分布式微服务架构（独立部署 ETL 服务、NLP 服务、图数据库服务、计价服务）**
  - *放弃原因*：工程经济所与项目现场经常需要在内网或单机工作站快速部署使用；过早拆分跨网络微服务会带来沉重的运维负担与序列化开销，不利于先通过进程内深模块收敛复杂度。
- **方案 C：微内核标准数据契约 + 六大深模块 + 插件注册表（选定方案）**
  - *选定原因*：兼具单体应用的极简部署特性与插件化架构的极致扩展性。任何新增格式、专业、算法或审查规则均遵循开闭原则（OCP），表现为新增单个 Adapter 文件或 YAML 配置文件。

## Consequences

- **单文件热插拔扩展**：新增一种造价文件解析器、一条 `BQ0xx` 审计规则或一个重排算法模型时，只需在对应模块的 `adapters/` 或 `rules/` 目录下新增一个实现对应 `Port` 协议的类并添加 `@Registry.register` 装饰器，核心流水线零代码改动。
- **接口即测试面（The Interface is the Test Surface）**：所有自动化测试直接针对六大深模块的入口契约编写，内部实现重构不会破坏既有测试用例。
- **契约先行约束**：任何跨模块的数据结构变更必须先更新 `src/core/contracts/` 中的 Pydantic 模型。
