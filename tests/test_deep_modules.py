from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
import openpyxl

from src.api.main import create_app
from src.core.contracts.boq_item import CanonicalProject, RawBoqItem
from src.core.contracts.matching import AuditSeverity, ConfidenceLevel, ExportOptions, MatchContext
from src.core.contracts.project import CostStage, EngineeringDomain, MaterialPriceRecord, ProjectContext, ProjectFileBundle
from src.infrastructure.repositories.sqlite_repo import SqliteBoqRepository
from src.modules.auditing.service import AuditLinterModule
from src.modules.exporter.service import ExporterModule
from src.modules.ingestion.service import IngestionModule
from src.modules.matching.service import PriceMatchingModule
from src.modules.pricing.service import PriceAdjustmentModule
from src.modules.standardizer.service import SemanticStandardizerModule

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def test_real_bundles_ingestion_and_total_closure():
    """契约测试 1：验证 data/ 下 3 套真实造价项目包 100% 无损解析与金额闭合."""
    ingestion = IngestionModule()

    # 1. 西柳沟生态综合治理工程 (Word + XLS 万元换算)
    proj1 = ingestion.ingest(
        ProjectFileBundle(
            files=[
                DATA_DIR / "西柳沟流域生态综合治理工程初步设计概算09.docx",
                DATA_DIR / "西柳沟流域生态综合治理工程10.19单行本导表.xls",
            ]
        )
    )
    assert proj1.context.province == "内蒙古自治区"
    assert proj1.context.price_base_month == "2026-07"
    assert len(proj1.items) == 327
    sum1_wan = sum(it.total_price_yuan or 0.0 for it in proj1.items) / 10000.0
    assert abs(sum1_wan - 12629.33) < 0.5
    heisha = next(it for it in proj1.items if "黑沙蒿" in it.raw_text_full)
    assert heisha.slots["species"].normalized_value == "黑沙蒿"
    assert heisha.slots["stem_diameter_cm"].numeric_value == 0.3
    assert heisha.slots["care_years"].numeric_value == 3.0

    # 2. 高明区全域土地综合整治项目 (DOC + 6表联动XLS)
    proj2 = ingestion.ingest(
        ProjectFileBundle(
            project_name="高明区明城镇全域土地综合整治项目（第四批次）",
            files=[
                DATA_DIR / "高明区明城镇全域土地综合整治项目（一标段.doc",
                DATA_DIR / "高明区明城镇全域土地综合整治项目第四批次.xls",
            ],
        )
    )
    assert proj2.context.province == "广东省"
    assert proj2.context.price_base_month == "2025-07"
    assert len(proj2.items) == 170
    sum2_yuan = sum(it.total_price_yuan or 0.0 for it in proj2.items)
    assert abs(sum2_yuan - 3097587.12) < 0.5
    earth_trans = next(it for it in proj2.items if it.seq_no == "1.1.1.2.2")
    assert earth_trans.breakdown is not None
    assert "10224" in earth_trans.breakdown.quota_codes
    assert earth_trans.slots["haul_distance_m"].numeric_value == 5000.0

    # 3. 二级泵站电缆综合单价 (13页 PDF 清单+主材表)
    proj3 = ingestion.ingest(
        ProjectFileBundle(files=[DATA_DIR / "二级泵站电缆综合单价.pdf"])
    )
    assert len(proj3.items) == 44
    assert len(proj3.context.base_materials) == 46
    sum3_yuan = sum(it.total_price_yuan or 0.0 for it in proj3.items)
    assert abs(sum3_yuan - 1794384.06) < 0.05
    c9 = next(it for it in proj3.items if it.seq_no == "9")
    assert c9.slots["cross_section_eq_mm2"].numeric_value == 545.0


def test_hard_spec_decay_prevents_cable_and_distance_mismatch():
    """契约测试 2：验证硬规格指数衰减核杜绝 3×150 错配 3×25 及运距 5km 错配 0.5km."""
    ingestion = IngestionModule()
    standardizer = SemanticStandardizerModule()
    matcher = PriceMatchingModule()

    p2 = ingestion.ingest(
        ProjectFileBundle(
            files=[
                DATA_DIR / "高明区明城镇全域土地综合整治项目（一标段.doc",
                DATA_DIR / "高明区明城镇全域土地综合整治项目第四批次.xls",
            ]
        )
    )
    p3 = ingestion.ingest(ProjectFileBundle(files=[DATA_DIR / "二级泵站电缆综合单价.pdf"]))
    matcher.build_index([*p2.items, *p3.items], [p2.context, p3.context])

    # 电缆 3×150+1×95 精确命中 1194.67 元，且对比纯 LSTM 基线验证衰减核作用
    q_cable = standardizer.standardize(
        RawBoqItem(
            seq_no="Q1",
            raw_name="电力电缆",
            raw_features="ZR-YJV-0.6/1KV-3×150+1×95 敷设方式:室内",
            raw_unit="m",
        ),
        domain=EngineeringDomain.INSTALLATION_GENERAL.value,
    )
    res_cable = matcher.match(q_cable, MatchContext(top_k=5, auto_adjust_price=False))
    assert res_cable.confidence == ConfidenceLevel.HIGH
    assert abs((res_cable.recommended_unit_price_yuan or 0.0) - 1194.67) < 0.01
    # 验证候选中的 3×25+1×16 被衰减核判定为硬冲突且得分受限
    small_cable = next((c for c in res_cable.candidates if c.candidate_item.seq_no == "10"), None)
    if small_cable:
        assert small_cable.has_hard_spec_conflict is True
        assert small_cable.hard_spec_kernel_score < 0.05

    # 土方运输 运距5km 精确匹配 21.63 元，而非 0.5km
    q_earth = standardizer.standardize(
        RawBoqItem(seq_no="Q2", raw_name="土（石）方运输，运距5km", raw_features="运距5km", raw_unit="m³"),
        domain=EngineeringDomain.LAND_CONSOLIDATION.value,
    )
    res_earth = matcher.match(q_earth, MatchContext(top_k=3, auto_adjust_price=False))
    assert abs((res_earth.recommended_unit_price_yuan or 0.0) - 21.63) < 0.01


def test_l2_material_differential_repricing():
    """契约测试 3：验证 L2 工料机主材价差动态重算算子 (柴油 7.0 -> 7.4 元/kg)."""
    ingestion = IngestionModule()
    standardizer = SemanticStandardizerModule()
    matcher = PriceMatchingModule(pricing_module=PriceAdjustmentModule())

    p2 = ingestion.ingest(
        ProjectFileBundle(
            files=[
                DATA_DIR / "高明区明城镇全域土地综合整治项目（一标段.doc",
                DATA_DIR / "高明区明城镇全域土地综合整治项目第四批次.xls",
            ]
        )
    )
    matcher.build_index(p2.items, [p2.context])

    target_ctx = ProjectContext(
        project_id="target_nm",
        project_name="内蒙古测试项目",
        domain=EngineeringDomain.LAND_CONSOLIDATION,
        province="内蒙古自治区",
        stage=CostStage.PRELIMINARY_DESIGN,
        price_base_month="2026-07",
        base_materials=[MaterialPriceRecord(material_name="柴油", unit="t", budget_price_excl_tax=7400.0)],
    )
    q_earth = standardizer.standardize(
        RawBoqItem(seq_no="Q1", raw_name="土（石）方运输，运距5km", raw_features="运距5km", raw_unit="m³"),
        domain=EngineeringDomain.LAND_CONSOLIDATION.value,
    )
    res = matcher.match(q_earth, MatchContext(target_project_ctx=target_ctx, top_k=3, auto_adjust_price=True))
    assert res.adjusted_price is not None
    assert res.adjusted_price.adjuster_used == "quota_material_diff"
    # 127.56 kg/100m³ * 0.4 元/kg * 1.09 税率 / 100 = +0.5562 元/m³ -> 21.63 + 0.5562 = 22.1862
    assert 22.15 <= res.adjusted_price.adjusted_unit_price_yuan <= 22.22


def test_auditing_and_excel_export(tmp_path: Path):
    """契约测试 4：验证 BQ001~BQ012 审查规则及原生 Excel 公式导出."""
    standardizer = SemanticStandardizerModule()
    auditor = AuditLinterModule()
    exporter = ExporterModule()

    bad_items = [
        standardizer.standardize(
            RawBoqItem(seq_no="1", raw_name="电力电缆", raw_features="室内敷设", raw_unit="m", quantity=10, unit_price_yuan=100, total_price_yuan=9999),
            domain=EngineeringDomain.INSTALLATION_GENERAL.value,
            project_id="test_p",
            idx=1,
        ),
        standardizer.standardize(
            RawBoqItem(seq_no="2", raw_name="土方开挖", raw_features="土方开挖", raw_unit="", quantity=50),
            domain=EngineeringDomain.LAND_CONSOLIDATION.value,
            project_id="test_p",
            idx=2,
        ),
    ]
    proj = CanonicalProject(
        context=ProjectContext(
            project_id="test_p",
            project_name="合规审查测试项目",
            domain=EngineeringDomain.INSTALLATION_GENERAL,
            province="陕西省",
            stage=CostStage.ESTIMATE,
            price_base_month="2026-11",
        ),
        items=bad_items,
    )
    findings = auditor.audit(proj)
    rule_ids = {f.rule_id for f in findings}
    assert "BQ002" in rule_ids  # 缺失单位
    assert "BQ004" in rule_ids  # 电缆缺截面积
    assert "BQ006" in rule_ids  # 10*100 != 9999

    proj.audit_findings = findings
    xlsx_bytes = exporter.export_estimate_excel(proj, ExportOptions(currency_unit="元", include_formula=True))
    wb = openpyxl.load_workbook(BytesIO(xlsx_bytes), data_only=False)
    assert "工程量清单估算表" in wb.sheetnames
    assert "智能匹配与定额溯源表" in wb.sheetnames
    assert "BQ合规与合理性审查报告" in wb.sheetnames
    ws = wb["工程量清单估算表"]
    formulas = [ws.cell(r, 8).value for r in range(4, ws.max_row + 1) if isinstance(ws.cell(r, 8).value, str)]
    assert any(f.startswith("=ROUND(") for f in formulas)
    assert any(f.startswith("=SUM(") for f in formulas)


def test_fastapi_endpoints_end_to_end(tmp_path: Path):
    """契约测试 5：验证 SQLite 双库引导与 FastAPI 全链路 API (总览/搜价/批量组价/校审回流/导出/评测)."""
    db_file = tmp_path / "test_boq.db"
    app = create_app(db_path=db_file, data_dir=DATA_DIR)
    client = TestClient(app)

    home = client.get("/")
    assert home.status_code == 200
    assert "BOQ Intelligence" in home.text

    sc = client.get("/showcase")
    assert sc.status_code == 200
    assert "组会技术架构全景汇报" in sc.text

    ov = client.get("/api/overview")
    assert ov.status_code == 200
    stats = ov.json()["stats"]
    assert stats["projects_count"] == 3
    assert stats["boq_items_count"] == 541  # 327 + 170 + 44
    assert stats["l2_breakdowns_count"] >= 80
    assert stats["materials_count"] >= 60

    sr = client.post("/api/match/search", json={"query_text": "电力电缆 ZR-YJV-0.6/1KV-3×150+1×95 室内"})
    assert sr.status_code == 200
    assert sr.json()["result"]["confidence"] == "high"

    br = client.post("/api/estimate/batch-preset", json={"preset_key": "cable_installation"})
    assert br.status_code == 200
    bdata = br.json()
    sess_id = bdata["session_id"]
    first_item = bdata["rows"][0]["item"]

    rv = client.post(
        f"/api/estimate/{sess_id}/review",
        json={"item_id": first_item["item_id"], "final_price": 265.0, "modify_reason": "合同调整", "persist_to_db": True},
    )
    assert rv.status_code == 200

    ex = client.get(f"/api/estimate/{sess_id}/export?currency_unit=万元")
    assert ex.status_code == 200
    assert len(ex.content) > 3000

    ev = client.post("/api/research/evaluate")
    assert ev.status_code == 200
    bms = {b["pipeline_name"]: b for b in ev.json()["benchmarks"]}
    assert bms["production_hybrid"]["top1_accuracy"] >= bms["lstm_cosine_baseline"]["top1_accuracy"]
    assert bms["production_hybrid"]["spec_conflict_rate_pct"] == 0.0

    # 验证知识图谱与标准提资模板下载及上传批量组价闭环
    kg = client.get("/api/knowledge-graph")
    assert kg.status_code == 200
    assert len(kg.json()["triples"]) >= 15

    tpl = client.get("/api/templates/takeoff")
    assert tpl.status_code == 200
    bu = client.post(
        "/api/estimate/batch-upload",
        data={"project_name": "模板上传测试项目", "domain": "07_installation", "province": "陕西省"},
        files={"file": ("takeoff.xlsx", tpl.content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert bu.status_code == 200
    assert len(bu.json()["rows"]) == 3

