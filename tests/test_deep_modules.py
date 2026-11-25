from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import openpyxl
import pytest

from src.api.main import create_app
from src.core.contracts.boq_item import CanonicalProject, RawBoqItem
from src.core.contracts.matching import AuditSeverity, ConfidenceLevel, ExportOptions, MatchContext
from src.core.contracts.project import CostStage, EngineeringDomain, MaterialPriceRecord, ProjectContext, ProjectFileBundle
from src.infrastructure.llm_client import LlmClientManager, normalize_openai_base_url
from src.infrastructure.repositories.sqlite_repo import SqliteBoqRepository
from src.modules.auditing.service import AuditLinterModule
from src.modules.exporter.service import ExporterModule
from src.modules.ingestion.service import IngestionModule
from src.modules.matching.service import PriceMatchingModule
from src.modules.pricing.service import PriceAdjustmentModule
from src.modules.standardizer.service import SemanticStandardizerModule

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture(autouse=True)
def _isolate_llm_settings(tmp_path: Path):
    """确保所有单元测试默认运行在隔离沙箱配置下，避免误触真实 data/llm_settings.json 外网请求."""
    LlmClientManager.reset_for_tests(settings_path=tmp_path / "isolated_llm_settings.json")
    try:
        yield
    finally:
        LlmClientManager.reset_for_tests()


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
    assert "settingsModal" in home.text

    sc = client.get("/showcase")
    assert sc.status_code == 200
    assert "组会技术架构全景汇报" in sc.text
    assert "showcaseSettingsDialog" in sc.text

    ov = client.get("/api/overview")
    assert ov.status_code == 200
    assert "llm" in ov.json()
    stats = ov.json()["stats"]
    assert stats["projects_count"] == 7
    assert stats["boq_items_count"] == 621  # 327 + 170 + 44 + 4*20
    assert stats["l2_breakdowns_count"] >= 500
    assert stats["materials_count"] >= 70

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
    assert bms["production_hybrid"]["cases_count"] == 42
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


def test_llm_relay_settings_and_model_fetching(tmp_path: Path):
    """契约测试 6：验证第三方中转站 URL 规范化、模型拉取、设置保存及槽位抽取集成."""
    assert normalize_openai_base_url("https://relay.example.com") == "https://relay.example.com/v1"
    assert normalize_openai_base_url("https://relay.example.com/v1/") == "https://relay.example.com/v1"
    assert normalize_openai_base_url("https://relay.example.com/v1/chat/completions") == "https://relay.example.com/v1"
    assert normalize_openai_base_url("127.0.0.1:11434") == "https://127.0.0.1:11434/v1"
    assert normalize_openai_base_url("http://127.0.0.1:11434") == "http://127.0.0.1:11434/v1"

    LlmClientManager.reset_for_tests(settings_path=tmp_path / "llm_settings.json")
    try:
        app = create_app(db_path=tmp_path / "llm_test_boq.db", data_dir=DATA_DIR)
        client = TestClient(app)

        mock_models_resp = MagicMock(status_code=200)
        mock_models_resp.json.return_value = {
            "data": [{"id": "gpt-4o-mini"}, {"id": "deepseek-chat"}, {"id": "qwen2.5-72b"}]
        }

        mock_chat_resp = MagicMock(status_code=200)
        mock_chat_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '```json\n{"slots":[{"slot_key":"impervious_level","slot_label":"抗渗等级","dimension":"entity_spec","normalized_value":"P8","numeric_value":8,"hard_constraint":true}]}\n```'
                    }
                }
            ]
        }

        with patch("src.infrastructure.llm_client.httpx.Client") as mock_client_cls:
            mock_http = mock_client_cls.return_value.__enter__.return_value
            mock_http.get.return_value = mock_models_resp
            mock_http.post.return_value = mock_chat_resp

            # 1) 根据第三方中转站 URL 拉取模型
            res_models = client.post(
                "/api/llm/models",
                json={"base_url": "https://my-relay.example.com/v1/chat/completions", "api_key": "sk-testsecret9988"},
            )
            assert res_models.status_code == 200
            mdata = res_models.json()
            assert mdata["normalized_base_url"] == "https://my-relay.example.com/v1"
            assert mdata["models"] == ["gpt-4o-mini", "deepseek-chat", "qwen2.5-72b"]
            mock_http.get.assert_called()

            # 2) 保存配置（留空 api_key 时保留已存 Key 且仅返回脱敏值）
            res_save = client.post(
                "/api/llm/settings",
                json={"enabled": True, "model": "deepseek-chat", "trigger_mode": "always"},
            )
            assert res_save.status_code == 200
            sdata = res_save.json()
            assert sdata["enabled"] is True
            assert sdata["model"] == "deepseek-chat"
            assert sdata["has_api_key"] is True
            assert sdata["api_key_masked"] == "sk-t****9988"
            assert "api_key" not in sdata

            # 3) 连通性试抽取测试
            res_test = client.post(
                "/api/llm/test",
                json={"sample_text": "P8 抗渗混凝土挡墙 现浇养护"},
            )
            assert res_test.status_code == 200
            tdata = res_test.json()
            assert tdata["ok"] is True
            assert "impervious_level" in tdata["slots"]
            assert tdata["slots"]["impervious_level"]["extractor_source"] == "llm:deepseek-chat"

            # 4) 验证 SemanticStandardizerModule 在启用后真实融入大模型抽取结果
            std_item = SemanticStandardizerModule().standardize(
                RawBoqItem(seq_no="L1", raw_name="抗渗混凝土挡墙", raw_features="P8抗渗混凝土 现浇", raw_unit="m³"),
                domain="01_water_ecology",
            )
            assert "impervious_level" in std_item.slots
            assert std_item.slots["impervious_level"].normalized_value == 8.0

            # 5) 验证全规则命中的标准预设批量组价不会逐条阻塞串行请求 LLM
            mock_http.post.reset_mock()
            br_fast = client.post("/api/estimate/batch-preset", json={"preset_key": "urban_water", "domain": "02_urban_water", "province": "陕西省"})
            assert br_fast.status_code == 200
            assert "elapsed_ms" in br_fast.json()
            assert mock_http.post.call_count == 0
    finally:
        LlmClientManager.reset_for_tests()


def test_session_persistence_and_restart_recovery(tmp_path: Path):
    """契约测试 7：验证批量估算会话落盘、校审修改持久化及模拟进程重启后的恢复与删除."""
    db_file = tmp_path / "sess_boq.db"
    app1 = create_app(db_path=db_file, data_dir=DATA_DIR)
    c1 = TestClient(app1)

    br = c1.post("/api/estimate/batch-preset", json={"preset_key": "cable_installation"})
    assert br.status_code == 200
    bdata = br.json()
    sess_id = bdata["session_id"]
    first_item = bdata["rows"][0]["item"]

    rv = c1.post(
        f"/api/estimate/{sess_id}/review",
        json={
            "item_id": first_item["item_id"],
            "final_price": 318.5,
            "modify_reason": "协议询价调定",
            "persist_to_db": False,
        },
    )
    assert rv.status_code == 200

    # 模拟服务完全重启：重新创建全新 FastAPI 容器（内存 sessions 为空）
    app2 = create_app(db_path=db_file, data_dir=DATA_DIR)
    c2 = TestClient(app2)

    ls = c2.get("/api/estimate/sessions")
    assert ls.status_code == 200
    sessions = ls.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == sess_id

    rec = c2.get(f"/api/estimate/sessions/{sess_id}")
    assert rec.status_code == 200
    rec_data = rec.json()
    rec_first = next(r["item"] for r in rec_data["rows"] if r["item"]["item_id"] == first_item["item_id"])
    assert abs(rec_first["normalized_unit_price_yuan"] - 318.5) < 0.01

    ex = c2.get(f"/api/estimate/{sess_id}/export?currency_unit=元")
    assert ex.status_code == 200
    assert len(ex.content) > 3000

    dl = c2.delete(f"/api/estimate/sessions/{sess_id}")
    assert dl.status_code == 200
    assert c2.get("/api/estimate/sessions").json()["sessions"] == []


def test_excel_inspect_and_multisheet_custom_mapping_upload(tmp_path: Path):
    """契约测试 8：验证任意异构多工作表 Excel 的预览探测、双层合并表头识别与自定义列映射上传组价."""
    import json as pyjson

    wb = openpyxl.Workbook()
    ws_cover = wb.active
    ws_cover.title = "编制说明与封面"
    ws_cover.append(["某某项目工程量清单提资文件"])
    ws_cover.append(["编制日期：2026年11月"])

    ws_civil = wb.create_sheet("土建分部提资")
    ws_civil.append(["某河道生态与土地整治提资表"])
    ws_civil.append(["细目号", "子目名称", "技术特征", "工程量", None, "备注"])
    ws_civil.merge_cells("D2:E2")
    ws_civil.append([None, None, None, "计量单位", "暂估工程量", None])
    ws_civil.append(["1", "土地平整工程", "", "", "", ""])
    ws_civil.append(["1.1", "清表，推土机推土", "地表清理", "m³", 1500.0, ""])
    ws_civil.append(["1.2", "土（石）方运输", "运距5km", "m³", 800.0, ""])

    ws_elec = wb.create_sheet("电气安装提资")
    ws_elec.append(["编号", "清单名称", "规格型号", "单位", "数量"])
    ws_elec.append(["E1", "电力电缆", "ZR-YJV-0.6/1KV-3×150+1×95 室内", "m", 100.0])

    buf = BytesIO()
    wb.save(buf)
    xlsx_bytes = buf.getvalue()

    app = create_app(db_path=tmp_path / "wiz_boq.db", data_dir=DATA_DIR)
    client = TestClient(app)

    insp = client.post(
        "/api/estimate/inspect-excel",
        files={"file": ("heterogeneous.xlsx", xlsx_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert insp.status_code == 200
    sheets_meta = {s["sheet_name"]: s for s in insp.json()["sheets"]}
    assert sheets_meta["编制说明与封面"]["auto_selected"] is False
    assert sheets_meta["土建分部提资"]["auto_selected"] is True
    assert sheets_meta["土建分部提资"]["header_row"] == 3
    assert sheets_meta["土建分部提资"]["detected_columns"]["name"] == 1
    assert sheets_meta["土建分部提资"]["detected_columns"]["qty"] == 4
    assert sheets_meta["电气安装提资"]["auto_selected"] is True

    mapping_spec = {
        "sheets": ["土建分部提资", "电气安装提资"],
        "sheet_overrides": {
            "土建分部提资": {
                "header_row": 3,
                "columns": {"seq": 0, "name": 1, "features": 2, "unit": 3, "qty": 4},
            },
            "电气安装提资": {
                "header_row": 1,
                "columns": {"seq": 0, "name": 1, "features": 2, "unit": 3, "qty": 4},
            },
        },
    }
    bu = client.post(
        "/api/estimate/batch-upload",
        data={
            "project_name": "多Sheet自定义映射测试工程",
            "domain": "05_land_consolidation",
            "province": "广东省",
            "mapping_json": pyjson.dumps(mapping_spec, ensure_ascii=False),
        },
        files={"file": ("heterogeneous.xlsx", xlsx_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert bu.status_code == 200
    rows = bu.json()["rows"]
    assert len(rows) == 3
    section_paths = [r["item"]["section_path"] for r in rows]
    assert any("土建分部提资" in sp for sp in section_paths)
    assert any("电气安装提资" in sp for sp in section_paths)


def test_multi_material_labor_and_copper_repricing(tmp_path: Path):
    """契约测试 9：验证 L2 商砼/钢筋/人工调差（防砂石重复计价）、L1 铜芯电缆物理截面联动调差及重算保留定稿行."""
    db_file = tmp_path / "reprice_boq.db"
    app = create_app(db_path=db_file, data_dir=DATA_DIR)
    client = TestClient(app)

    # 1. 测试铜芯电缆按电解铜价 (82000 元/t vs 基准 72000 元/t) 联动重算
    sr_cu = client.post(
        "/api/match/search",
        json={
            "query_text": "电力电缆 ZR-YJV-0.6/1KV-3×150+1×95 室内",
            "domain": "07_installation",
            "unit": "m",
            "target_copper_yuan_per_t": 82000.0,
        },
    )
    assert sr_cu.status_code == 200
    adj_cu = sr_cu.json()["result"]["adjusted_price"]
    assert adj_cu["adjuster_used"] == "copper_cable_diff"
    # 545 mm² * 8.89e-6 * 1.025 * 10000 元/t = +49.66 元/m -> 1194.67 + 49.66 ≈ 1244.33
    assert 1240.0 <= adj_cu["adjusted_unit_price_yuan"] <= 1250.0

    # 2. 测试 L2 商品砼 C25 + 综合人工工日协同调差（并验证砂子与水泥砂浆不混淆）
    sr_conc = client.post(
        "/api/match/search",
        json={
            "query_text": "C25混凝土 拉梁 双轮胶车运至施工点 综合运距为250m",
            "domain": "05_land_consolidation",
            "unit": "m³",
            "target_labor_yuan_per_day": 140.0,
            "target_materials": [
                {"material_name": "商品混凝土", "unit": "m³", "price": 450.0},
                {"material_name": "砂", "unit": "m³", "price": 193.96},
            ],
        },
    )
    assert sr_conc.status_code == 200
    adj_conc = sr_conc.json()["result"]["adjusted_price"]
    assert adj_conc["adjuster_used"] == "quota_material_diff"
    diff_txt = " ".join(adj_conc["price_diff_details"])
    assert "商品砼 C25" in diff_txt
    assert "工日调差" in diff_txt
    assert "水泥砂浆" not in diff_txt

    # 3. 测试批量组价 -> 人工定稿第1行 -> 全表 /reprice 重算（保留已定稿项，重算未定稿柴油运输项）
    br = client.post(
        "/api/estimate/batch-preset",
        json={
            "preset_key": "land_consolidation",
            "domain": "05_land_consolidation",
            "province": "广东省",
            "diesel_yuan_per_kg": 7.4,
        },
    )
    assert br.status_code == 200
    bdata = br.json()
    sess_id = bdata["session_id"]

    first_item = bdata["rows"][0]["item"]
    earth_item_id = next(r["item"]["item_id"] for r in bdata["rows"] if "运距5km" in r["item"]["raw_text_full"])
    old_earth_price = next(r["item"]["normalized_unit_price_yuan"] for r in bdata["rows"] if r["item"]["item_id"] == earth_item_id)

    client.post(
        f"/api/estimate/{sess_id}/review",
        json={"item_id": first_item["item_id"], "final_price": 9.99, "modify_reason": "总工审定", "persist_to_db": False},
    )
    rp = client.post(
        f"/api/estimate/{sess_id}/reprice",
        json={"diesel_yuan_per_kg": 8.0, "overwrite_reviewed": False},
    )
    assert rp.status_code == 200
    rep_rows = rp.json()["rows"]
    rep_first = next(r["item"] for r in rep_rows if r["item"]["item_id"] == first_item["item_id"])
    rep_earth = next(r["item"] for r in rep_rows if r["item"]["item_id"] == earth_item_id)
    assert abs(rep_first["normalized_unit_price_yuan"] - 9.99) < 0.001
    assert rep_earth["normalized_unit_price_yuan"] > old_earth_price


def test_yaml_driven_bq004_and_bq013_companion_omission():
    """契约测试 10：验证 BQ004 全领域 YAML 自动驱动硬槽位缺失检查与 BQ013 配套工序漏项检查."""
    standardizer = SemanticStandardizerModule()
    auditor = AuditLinterModule()

    # 用例 A：02_urban_water 缺管径和环刚度 + 埋地 PE管 缺沟槽土方 -> 应同时触发 BQ004 和 BQ013
    pipe_only = [
        standardizer.standardize(
            RawBoqItem(seq_no="1", raw_name="HDPE双壁波纹管", raw_features="HDPE双壁波纹管 室外排水敷设", raw_unit="m", quantity=120.0),
            domain=EngineeringDomain.URBAN_WATER.value,
            project_id="audit_a",
            idx=1,
        ),
        standardizer.standardize(
            RawBoqItem(seq_no="2", raw_name="现浇混凝土渠道衬砌", raw_features="现浇混凝土渠道衬砌", raw_unit="m³", quantity=50.0),
            domain=EngineeringDomain.IRRIGATION.value,
            project_id="audit_a",
            idx=2,
        ),
    ]
    proj_a = CanonicalProject(
        context=ProjectContext(
            project_id="audit_a",
            project_name="全领域YAML规则测试工程",
            domain=EngineeringDomain.URBAN_WATER,
            province="陕西省",
            stage=CostStage.ESTIMATE,
        ),
        items=pipe_only,
    )
    findings_a = auditor.audit(proj_a)
    bq004_msgs = [f.message for f in findings_a if f.rule_id == "BQ004"]
    bq013_msgs = [f.message for f in findings_a if f.rule_id == "BQ013"]
    assert any("HDPE双壁波纹管" in m and "公称直径" in m for m in bq004_msgs)
    assert any("渠道衬砌" in m and "混凝土标号" in m for m in bq004_msgs)
    assert any("沟槽" in m for m in bq013_msgs)

    # 用例 B：补齐沟槽土方开挖与回填后，埋地管道漏项提示自动消除
    with_trench = [
        pipe_only[0],
        standardizer.standardize(
            RawBoqItem(seq_no="2", raw_name="沟槽土方开挖及回填", raw_features="管沟开挖土方并回填", raw_unit="m³", quantity=240.0),
            domain=EngineeringDomain.URBAN_WATER.value,
            project_id="audit_b",
            idx=2,
        ),
    ]
    proj_b = CanonicalProject(
        context=ProjectContext(
            project_id="audit_b",
            project_name="配套完整工程",
            domain=EngineeringDomain.URBAN_WATER,
            province="陕西省",
            stage=CostStage.ESTIMATE,
        ),
        items=with_trench,
    )
    findings_b = auditor.audit(proj_b)
    assert not any(f.rule_id == "BQ013" and "沟槽" in f.message for f in findings_b)


def test_all_seven_domains_ontologies_and_matching(tmp_path: Path):
    """契约测试 11：验证 01~07 全七大工程领域本体深度、基准工程样本库及一键预设组价全覆盖."""
    app = create_app(db_path=tmp_path / "seven_domains.db", data_dir=DATA_DIR)
    client = TestClient(app)

    ov = client.get("/api/overview").json()
    assert len(ov["domains"]) == 7
    ontologies = ov["ontologies"]
    for dom in ("02_urban_water", "03_irrigation", "04_coal_mine_water", "06_marine_ecology"):
        assert dom in ontologies
        assert ontologies[dom]["categories"] == 3

    for preset_key, dom, prov in (
        ("urban_water", "02_urban_water", "陕西省"),
        ("irrigation", "03_irrigation", "宁夏回族自治区"),
        ("coal_mine_water", "04_coal_mine_water", "陕西省"),
        ("marine_ecology", "06_marine_ecology", "广东省"),
    ):
        res = client.post(
            "/api/estimate/batch-preset",
            json={"preset_key": preset_key, "domain": dom, "province": prov, "stage": "投资估算"},
        )
        assert res.status_code == 200
        body = res.json()
        assert len(body["rows"]) == 5
        assert body["total_yuan"] > 0
        assert body["investment_summary"]["total_investment_yuan"] > body["total_yuan"]
        assert all(r["match"]["confidence"] == "high" for r in body["rows"])


def test_full_investment_summary_five_sheet_excel_export():
    """契约测试 12：验证 5 表联动全费用投资估算总表、措施项目取费表与跨表动态 Excel 公式闭合."""
    standardizer = SemanticStandardizerModule()
    exporter = ExporterModule()
    items = [
        standardizer.standardize(
            RawBoqItem(
                seq_no="1.1",
                section_path=["市政管网工程"],
                raw_name="HDPE双壁波纹管安装",
                raw_features="HDPE双壁波纹管 DN400 SN8",
                raw_unit="m",
                quantity=500.0,
                unit_price_yuan=268.5,
                total_price_yuan=134250.0,
            ),
            domain=EngineeringDomain.URBAN_WATER.value,
            project_id="exp_5sheet",
            idx=1,
        )
    ]
    proj = CanonicalProject(
        context=ProjectContext(
            project_id="exp_5sheet",
            project_name="五表联动测试项目",
            domain=EngineeringDomain.URBAN_WATER,
            province="陕西省",
            stage=CostStage.PRELIMINARY_DESIGN,
            price_base_month="2026-11",
        ),
        items=items,
    )
    xlsx_bytes = exporter.export_estimate_excel(
        proj,
        ExportOptions(
            currency_unit="万元",
            include_formula=True,
            measure_rate_pct=4.5,
            other_project_rate_pct=11.5,
            contingency_rate_pct=5.0,
        ),
    )
    wb = openpyxl.load_workbook(BytesIO(xlsx_bytes), data_only=False)
    assert wb.sheetnames == [
        "工程总投资估算汇总表",
        "工程量清单估算表",
        "措施项目与取费表",
        "智能匹配与定额溯源表",
        "BQ合规与合理性审查报告",
    ]
    ws_sum = wb["工程总投资估算汇总表"]
    assert str(ws_sum["D4"].value) == "=D5+D6"
    assert str(ws_sum["D5"].value).startswith("='工程量清单估算表'!H")
    assert str(ws_sum["D6"].value) == "='措施项目与取费表'!F6"
    assert str(ws_sum["D16"].value) == "=D14+D15"

    ws_meas = wb["措施项目与取费表"]
    assert str(ws_meas["F6"].value) == "=SUM(F3:F5)"
    assert str(ws_meas["D3"].value).startswith("='工程量清单估算表'!H")


def test_dense_bge_sqlite_persistence_and_reload(tmp_path: Path, monkeypatch) -> None:
    """验证 BGE 真实向量编码、SQLite boq_embeddings 持久化写入及冷启动秒级复用（无本地权重时安全退避 SVD）."""
    import numpy as np
    from src.infrastructure.repositories.sqlite_repo import SqliteBoqRepository
    from src.modules.ingestion.service import IngestionModule
    from src.modules.matching.retrievers.dense_retriever import DenseSemanticRetriever, _find_local_hf_snapshot
    from src.modules.standardizer.service import SemanticStandardizerModule

    db_file = tmp_path / "bge_persist.db"
    repo = SqliteBoqRepository(db_path=db_file)
    ingestion = IngestionModule(standardizer=SemanticStandardizerModule())
    with LlmClientManager.get_instance().offline_scope():
        repo.bootstrap_sample_projects(data_dir=DATA_DIR, ingestion_module=ingestion)
    _projs, items = repo.load_all_projects_and_items()
    sample = items[:16]

    has_local_bge = (_find_local_hf_snapshot("BAAI/bge-m3") is not None) or (
        _find_local_hf_snapshot("BAAI/bge-base-zh-v1.5") is not None
    )
    if has_local_bge:
        monkeypatch.setenv("BOQ_ENABLE_NEURAL_IN_TESTS", "1")
        ret1 = DenseSemanticRetriever(backend="auto")
        ret1.build_index(sample, repo=repo)
        st1 = ret1.status_info()
        assert st1["backend_kind"] == "bge"
        assert st1["dim"] in (768, 1024)
        assert st1["indexed_items"] == len(sample)

        emb_stats = repo.get_embedding_stats()
        assert emb_stats and emb_stats[0]["model_name"] == st1["model_name"]
        assert emb_stats[0]["cnt"] == len(sample)

        ids1, mat1 = ret1.export_doc_matrix()
        assert mat1 is not None and len(ids1) == len(sample)

        ret2 = DenseSemanticRetriever(backend="auto")
        ret2.build_index(sample, repo=repo)
        ids2, mat2 = ret2.export_doc_matrix()
        assert ids1 == ids2 and mat2 is not None
        assert np.allclose(mat1, mat2, atol=1e-5)
    else:
        ret_svd = DenseSemanticRetriever(backend="svd")
        ret_svd.build_index(sample, repo=repo)
        assert ret_svd.status_info()["backend_kind"] == "svd"
        dummy = np.ones((len(sample), 8), dtype=np.float32)
        repo.save_embeddings(
            "ci-mock-bge",
            [(it.item_id, f"h{idx}", 8, dummy[idx].tobytes()) for idx, it in enumerate(sample)],
        )
        assert repo.get_embedding_stats()[0]["cnt"] == len(sample)


def test_trainable_rgcn_and_lopo_cv_api(tmp_path: Path, monkeypatch) -> None:
    """验证 2 层可训练 R-GCN 图谱拟合降损、检查点落盘与 5 折零泄露 LOPO-CV + Bootstrap 显著性接口."""
    import src.api.main as api_main
    import src.modules.research.rgcn_trainer as rgcn_mod

    ckpt_file = tmp_path / "rgcn_test_ckpt.pt"
    monkeypatch.setattr(api_main, "DEFAULT_RGCN_CKPT_PATH", ckpt_file)
    monkeypatch.setattr(rgcn_mod, "DEFAULT_RGCN_CKPT_PATH", ckpt_file)

    app = create_app(db_path=tmp_path / "rgcn_lopo.db", data_dir=DATA_DIR)
    client = TestClient(app)

    ov = client.get("/api/overview")
    assert ov.status_code == 200
    ov_data = ov.json()
    assert "research_status" in ov_data
    assert ov_data["research_status"]["dense"]["indexed_items"] >= 500

    train_res = client.post("/api/research/train-rgcn", json={"epochs": 25, "hidden_dim": 64})
    assert train_res.status_code == 200
    tr_data = train_res.json()
    assert tr_data["status"] == "trained"
    assert ckpt_file.exists()
    tm = tr_data["train_metrics"]
    assert tm["graph_nodes"] >= 600 and tm["graph_edges"] >= 5000
    assert tm["loss_history"][-1]["loss"] < tm["loss_history"][0]["loss"]
    assert tm["learned_alpha"] >= 1.5
    assert tr_data["research_status"]["rgcn"]["ckpt_loaded"] is True

    lopo_res = client.post("/api/research/lopo-cv", json={"train_rgcn_per_fold": True, "rgcn_epochs": 15})
    assert lopo_res.status_code == 200
    lp = lopo_res.json()
    assert lp["folds_count"] == 5
    total_items = ov_data["stats"]["boq_items_count"]
    for f in lp["folds"]:
        assert f["train_count"] + f["holdout_count"] == total_items

    by_pipe = {row["pipeline_name"]: row for row in lp["overall_summary"]}
    assert by_pipe["production_hybrid"]["top1_accuracy"] >= 85.0
    assert by_pipe["rgcn_graph_reranker"]["top1_accuracy"] >= 85.0
    assert by_pipe["production_hybrid"]["top1_vs_baseline"]["significant_001"] is True
    assert by_pipe["rgcn_graph_reranker"]["top1_vs_baseline"]["p_value"] <= 0.01


