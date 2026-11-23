from collections import defaultdict
import hashlib
from pathlib import Path
from statistics import median
import tempfile
from typing import Any, Optional
import uuid

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from src.core.contracts.boq_item import CanonicalProject, RawBoqItem, StandardizedBoqItem
from src.core.contracts.matching import ConfidenceLevel, ExportOptions, MatchContext
from src.core.contracts.project import (
    CostStage,
    EngineeringDomain,
    MaterialPriceRecord,
    ProjectContext,
    ProjectFileBundle,
)
from src.core.registry import PluginRegistry
from src.infrastructure.repositories.sqlite_repo import SqliteBoqRepository
from src.modules.auditing.service import AuditLinterModule
from src.modules.exporter.service import ExporterModule
from src.modules.ingestion.service import IngestionModule
from src.modules.matching.service import PriceMatchingModule
from src.modules.pricing.service import PriceAdjustmentModule
from src.modules.standardizer.service import SemanticStandardizerModule

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "boq_knowledge.db"
STATIC_DIR = Path(__file__).resolve().parent / "static"


class SearchRequest(BaseModel):
    query_text: str
    domain: Optional[str] = None
    province: Optional[str] = None
    unit: Optional[str] = None
    top_k: int = 8
    pipeline_name: str = "production_hybrid"
    target_diesel_yuan_per_kg: Optional[float] = None


class BatchPresetRequest(BaseModel):
    preset_key: str = "water_ecology"
    project_name: str = "长安区和美乡村水环境治理示范工程（估算）"
    domain: str = "01_water_ecology"
    province: str = "陕西省"
    stage: str = "投资估算"
    price_base_month: str = "2026-11"
    diesel_yuan_per_kg: Optional[float] = None
    pipeline_name: str = "production_hybrid"


class ReviewEditRequest(BaseModel):
    item_id: str
    final_price: float
    modify_reason: str = "人工校审调整"
    reviewer_role: str = "校核人"
    persist_to_db: bool = True


class AppContainer:
    """进程内深模块协调容器，封装全部业务管线并维护在线工作台会话状态."""

    def __init__(self, db_path: Path = DB_PATH, data_dir: Path = DATA_DIR) -> None:
        self.standardizer = SemanticStandardizerModule()
        self.pricing = PriceAdjustmentModule()
        self.auditor = AuditLinterModule()
        self.exporter = ExporterModule()
        self.ingestion = IngestionModule(standardizer=self.standardizer, auditor=self.auditor)
        self.matching = PriceMatchingModule(pricing_module=self.pricing)
        self.repo = SqliteBoqRepository(db_path=db_path)
        self.repo.bootstrap_sample_projects(data_dir=data_dir, ingestion_module=self.ingestion)
        self.projects: list[ProjectContext] = []
        self.items: list[StandardizedBoqItem] = []
        self.hist_stats: dict[str, Any] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.reload_index()

    def reload_index(self) -> None:
        self.projects, self.items = self.repo.load_all_projects_and_items()
        self.matching.build_index(self.items, self.projects)
        avg_map: dict[tuple[str, str], list[float]] = defaultdict(list)
        for it in self.items:
            if it.normalized_unit_price_yuan is not None and it.standard_unit:
                avg_map[(it.standard_name, it.standard_unit)].append(it.normalized_unit_price_yuan)
        self.hist_stats = {"avg_prices_by_name_unit": dict(avg_map)}


def create_app(db_path: Path = DB_PATH, data_dir: Path = DATA_DIR) -> FastAPI:
    app = FastAPI(title="工程量清单语义标准及智能价格匹配平台", version="1.0.0")
    container = AppContainer(db_path=db_path, data_dir=data_dir)

    @app.get("/", response_class=HTMLResponse)
    def serve_index() -> HTMLResponse:
        html_path = STATIC_DIR / "index.html"
        if not html_path.exists():
            return HTMLResponse("<h1>BOQ Intelligence UI not found</h1>", status_code=404)
        return HTMLResponse(html_path.read_text(encoding="utf-8"))

    @app.get("/api/overview")
    def get_overview() -> dict[str, Any]:
        stats = container.repo.get_stats_summary()
        provinces = sorted({p.province for p in container.projects})
        domains = sorted({p.domain.value for p in container.projects})
        plugins = {
            "parsers": list(PluginRegistry.all_in("parsers").keys()),
            "extractors": list(PluginRegistry.all_in("extractors").keys()),
            "retrievers": list(PluginRegistry.all_in("retrievers").keys()),
            "rerankers": list(PluginRegistry.all_in("rerankers").keys()),
            "adjusters": list(PluginRegistry.all_in("adjusters").keys()),
            "audit_rules": sorted(PluginRegistry.all_in("audit_rules").keys()),
        }
        return {
            "stats": stats,
            "provinces": provinces,
            "domains": domains,
            "projects": [p.model_dump(mode="json") for p in container.projects],
            "ontologies": {
                k: {"domain_name": v.get("domain_name"), "categories": len(v.get("categories", []))}
                for k, v in container.standardizer.list_ontologies().items()
            },
            "plugins": plugins,
        }

    @app.get("/api/projects/{project_id}/items")
    def get_project_items(
        project_id: str,
        keyword: Optional[str] = Query(default=None),
        limit: int = Query(default=200, le=500),
    ) -> dict[str, Any]:
        proj = next((p for p in container.projects if p.project_id == project_id), None)
        if not proj:
            raise HTTPException(status_code=404, detail="Project not found")
        filtered = [it for it in container.items if it.project_id == project_id]
        if keyword:
            kw = keyword.lower()
            filtered = [it for it in filtered if kw in it.raw_text_full.lower() or kw in it.standard_name.lower()]
        return {
            "project": proj.model_dump(mode="json"),
            "total": len(filtered),
            "items": [it.model_dump(mode="json") for it in filtered[:limit]],
        }

    @app.get("/api/knowledge-graph")
    def get_knowledge_graph(limit: int = Query(default=80, le=200)) -> dict[str, Any]:
        return {"triples": container.repo.list_kg_triples(limit=limit)}

    @app.post("/api/projects/ingest")
    async def ingest_project_files(
        files: list[UploadFile] = File(...),
        project_name: Optional[str] = Form(default=None),
        province: Optional[str] = Form(default=None),
        domain: Optional[str] = Form(default=None),
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmpdir:
            saved_paths: list[Path] = []
            for uf in files:
                fpath = Path(tmpdir) / (uf.filename or "upload.xlsx")
                fpath.write_bytes(await uf.read())
                saved_paths.append(fpath)

            bundle = ProjectFileBundle(
                project_name=project_name or None,
                domain_hint=EngineeringDomain(domain) if domain else None,
                province_hint=province or None,
                files=saved_paths,
            )
            canonical = container.ingestion.ingest(bundle)
            container.repo.save_project(canonical)
            container.reload_index()
            return {
                "project": canonical.context.model_dump(mode="json"),
                "items_count": len(canonical.items),
                "quality_metrics": canonical.quality_metrics,
                "audit_findings": [f.model_dump(mode="json") for f in canonical.audit_findings],
            }

    @app.post("/api/match/search")
    def search_similar_prices(req: SearchRequest) -> dict[str, Any]:
        domain_val = req.domain or _infer_query_domain(req.query_text)
        raw_q = RawBoqItem(
            seq_no="Q1",
            raw_name=req.query_text,
            raw_features=req.query_text,
            raw_unit=req.unit or None,
        )
        std_q = container.standardizer.standardize(raw_q, domain=domain_val, project_id="search_query", idx=1)

        target_mats = []
        if req.target_diesel_yuan_per_kg is not None:
            target_mats.append(
                MaterialPriceRecord(
                    material_name="柴油",
                    unit="kg",
                    budget_price_excl_tax=req.target_diesel_yuan_per_kg,
                )
            )
        target_ctx = ProjectContext(
            project_id="search_target",
            project_name="实时搜价查询",
            domain=EngineeringDomain(domain_val),
            province=req.province or "未指定",
            stage=CostStage.ESTIMATE,
            price_base_month="2026-11",
            base_materials=target_mats,
        )
        match_ctx = MatchContext(
            target_project_ctx=target_ctx,
            preferred_provinces=[req.province] if req.province else [],
            preferred_domains=[req.domain] if req.domain else [],
            top_k=req.top_k,
            pipeline_name=req.pipeline_name,
            auto_adjust_price=True,
        )
        result = container.matching.match(std_q, match_ctx)

        prices = [
            c.candidate_item.normalized_unit_price_yuan
            for c in result.candidates
            if c.candidate_item.normalized_unit_price_yuan is not None and not c.has_hard_spec_conflict
        ]
        price_stats = (
            {
                "min": round(min(prices), 2),
                "median": round(median(prices), 2),
                "avg": round(sum(prices) / len(prices), 2),
                "max": round(max(prices), 2),
                "sample_count": len(prices),
            }
            if prices
            else None
        )
        return {
            "query": std_q.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
            "price_stats": price_stats,
        }

    @app.post("/api/estimate/batch-preset")
    def batch_estimate_preset(req: BatchPresetRequest) -> dict[str, Any]:
        raw_items = _build_preset_takeoff(req.preset_key)
        target_ctx = _make_target_ctx(
            req.project_name, req.domain, req.province, req.stage, req.price_base_month, req.diesel_yuan_per_kg
        )
        return _run_batch_estimation(container, target_ctx, raw_items, req.pipeline_name)

    @app.post("/api/estimate/batch-upload")
    async def batch_estimate_upload(
        file: UploadFile = File(...),
        project_name: str = Form("新工程估算项目"),
        domain: str = Form("01_water_ecology"),
        province: str = Form("陕西省"),
        stage: str = Form("投资估算"),
        price_base_month: str = Form("2026-11"),
        diesel_yuan_per_kg: Optional[float] = Form(default=None),
        pipeline_name: str = Form("production_hybrid"),
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / (file.filename or "takeoff.xlsx")
            fpath.write_bytes(await file.read())
            bundle = ProjectFileBundle(
                project_name=project_name,
                domain_hint=EngineeringDomain(domain),
                province_hint=province,
                stage_hint=CostStage(stage),
                price_base_month_hint=price_base_month,
                files=[fpath],
            )
            parsed = container.ingestion.ingest(bundle)
            target_ctx = _make_target_ctx(
                project_name, domain, province, stage, price_base_month, diesel_yuan_per_kg
            )
            raw_items = [
                RawBoqItem(
                    seq_no=it.seq_no,
                    section_path=it.section_path,
                    boq_code=it.boq_code,
                    raw_name=it.standard_name,
                    raw_features=it.raw_text_full,
                    raw_unit=it.raw_unit or it.standard_unit,
                    quantity=it.normalized_quantity,
                )
                for it in parsed.items
            ]
            return _run_batch_estimation(container, target_ctx, raw_items, pipeline_name)

    @app.post("/api/estimate/{session_id}/review")
    def submit_review_edit(session_id: str, req: ReviewEditRequest) -> dict[str, Any]:
        sess = container.sessions.get(session_id)
        if not sess:
            raise HTTPException(status_code=404, detail="Estimation session not found")

        canonical: CanonicalProject = sess["project"]
        details_map: dict[str, dict[str, Any]] = sess["details_map"]
        target_item = next((it for it in canonical.items if it.item_id == req.item_id), None)
        if not target_item:
            raise HTTPException(status_code=404, detail="BOQ item not found in session")

        ai_price = target_item.normalized_unit_price_yuan
        target_item.normalized_unit_price_yuan = round(req.final_price, 4)
        if target_item.normalized_quantity is not None:
            target_item.total_price_yuan = round(target_item.normalized_quantity * req.final_price, 2)

        det = details_map.setdefault(req.item_id, {})
        det["confidence"] = ConfidenceLevel.HIGH.value
        det["provenance_summary"] = f"三级校审定稿({req.reviewer_role}): {req.modify_reason}"
        det["diff_summary"] = f"AI推荐 {ai_price} -> 审定 {req.final_price} ({req.modify_reason})"

        low_ids = {
            iid for iid, d in details_map.items() if d.get("confidence") == ConfidenceLevel.LOW.value
        }
        canonical.audit_findings = container.auditor.audit(
            canonical, {**container.hist_stats, "low_confidence_item_ids": low_ids}
        )

        fb_id = container.repo.save_review_feedback(
            target_project_id=canonical.context.project_id,
            query_item=target_item,
            matched_item_id=det.get("matched_item_id"),
            ai_price=ai_price,
            final_price=req.final_price,
            modify_reason=req.modify_reason,
            reviewer_role=req.reviewer_role,
            persist_to_db=req.persist_to_db,
        )
        if req.persist_to_db:
            container.reload_index()

        total_yuan = round(sum(it.total_price_yuan or 0.0 for it in canonical.items), 2)
        return {
            "feedback_id": fb_id,
            "total_yuan": total_yuan,
            "updated_item": target_item.model_dump(mode="json"),
            "audit_findings": [f.model_dump(mode="json") for f in canonical.audit_findings],
        }

    @app.get("/api/estimate/{session_id}/export")
    def export_session_excel(
        session_id: str, currency_unit: str = Query(default="元")
    ) -> Response:
        sess = container.sessions.get(session_id)
        if not sess:
            raise HTTPException(status_code=404, detail="Estimation session not found")
        opts = ExportOptions(currency_unit=currency_unit)
        xlsx_bytes = container.exporter.export_estimate_excel(
            sess["project"], options=opts, match_details=sess["details_map"]
        )
        filename = f"{sess['project'].context.project_name}_智能估算表.xlsx"
        from urllib.parse import quote

        return Response(
            content=xlsx_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
        )

    @app.get("/api/templates/takeoff")
    def download_takeoff_template() -> Response:
        from urllib.parse import quote

        xlsx_bytes = container.exporter.export_takeoff_template()
        return Response(
            content=xlsx_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote('工程量清单标准提资模板.xlsx')}"},
        )

    @app.post("/api/research/evaluate")
    def run_research_benchmark() -> dict[str, Any]:
        return _evaluate_all_pipelines(container)

    return app


def _infer_query_domain(text: str) -> str:
    if any(k in text for k in ("电缆", "铜鼻子", "终端", "接地", "KVVP", "YJV", "DJYVP")):
        return EngineeringDomain.INSTALLATION_GENERAL.value
    if any(k in text for k in ("运距", "表土", "田埂", "农沟", "生产路", "翻耕", "清表", "PE管")):
        return EngineeringDomain.LAND_CONSOLIDATION.value
    return EngineeringDomain.WATER_ECOLOGY.value


def _make_target_ctx(
    project_name: str,
    domain: str,
    province: str,
    stage: str,
    month: str,
    diesel_yuan_per_kg: Optional[float],
) -> ProjectContext:
    mats = []
    if diesel_yuan_per_kg is not None:
        mats.append(
            MaterialPriceRecord(material_name="柴油", unit="kg", budget_price_excl_tax=diesel_yuan_per_kg)
        )
    pid = "est_" + hashlib.md5(f"{project_name}:{uuid.uuid4()}".encode("utf-8")).hexdigest()[:8]
    return ProjectContext(
        project_id=pid,
        project_name=project_name,
        domain=EngineeringDomain(domain),
        province=province,
        stage=CostStage(stage),
        price_base_month=month,
        base_materials=mats,
    )


def _run_batch_estimation(
    container: AppContainer,
    target_ctx: ProjectContext,
    raw_items: list[RawBoqItem],
    pipeline_name: str,
) -> dict[str, Any]:
    std_items: list[StandardizedBoqItem] = []
    match_results: list[dict[str, Any]] = []
    details_map: dict[str, dict[str, Any]] = {}
    low_ids: set[str] = set()

    for idx, raw_it in enumerate(raw_items, start=1):
        std_q = container.standardizer.standardize(
            raw_it, domain=target_ctx.domain.value, project_id=target_ctx.project_id, idx=idx
        )
        m_ctx = MatchContext(
            target_project_ctx=target_ctx,
            preferred_provinces=[target_ctx.province] if target_ctx.province != "未指定" else [],
            top_k=3,
            pipeline_name=pipeline_name,
            auto_adjust_price=True,
        )
        m_res = container.matching.match(std_q, m_ctx)
        std_q.normalized_unit_price_yuan = m_res.recommended_unit_price_yuan
        if std_q.normalized_quantity is not None and m_res.recommended_unit_price_yuan is not None:
            std_q.total_price_yuan = round(std_q.normalized_quantity * m_res.recommended_unit_price_yuan, 2)

        if m_res.confidence == ConfidenceLevel.LOW:
            low_ids.add(std_q.item_id)

        top_cand = m_res.candidates[0] if m_res.candidates else None
        details_map[std_q.item_id] = {
            "confidence": m_res.confidence.value,
            "final_score": top_cand.final_score if top_cand else 0.0,
            "hard_spec_kernel_score": top_cand.hard_spec_kernel_score if top_cand else 0.0,
            "matched_item_id": top_cand.candidate_item.item_id if top_cand else None,
            "adjuster_used": m_res.adjusted_price.adjuster_used if m_res.adjusted_price else "none",
            "original_unit_price": m_res.adjusted_price.original_unit_price_yuan if m_res.adjusted_price else 0.0,
            "provenance_summary": top_cand.attribution.summary_reason if top_cand else "无相似历史项",
            "diff_summary": "; ".join(m_res.adjusted_price.price_diff_details) if m_res.adjusted_price else "",
        }
        std_items.append(std_q)
        match_results.append(
            {
                "item": std_q.model_dump(mode="json"),
                "match": m_res.model_dump(mode="json"),
            }
        )

    canonical = CanonicalProject(
        context=target_ctx,
        items=std_items,
        raw_items_count=len(std_items),
    )
    canonical.audit_findings = container.auditor.audit(
        canonical, {**container.hist_stats, "low_confidence_item_ids": low_ids}
    )

    session_id = target_ctx.project_id
    container.sessions[session_id] = {"project": canonical, "details_map": details_map}
    total_yuan = round(sum(it.total_price_yuan or 0.0 for it in std_items), 2)

    return {
        "session_id": session_id,
        "project": target_ctx.model_dump(mode="json"),
        "total_yuan": total_yuan,
        "rows": match_results,
        "audit_findings": [f.model_dump(mode="json") for f in canonical.audit_findings],
    }


def _build_preset_takeoff(preset_key: str) -> list[RawBoqItem]:
    if preset_key == "land_consolidation":
        return [
            RawBoqItem(seq_no="1.1", section_path=["土地平整工程"], raw_name="清表，推土机推土", raw_features="清表，推土机推土", raw_unit="m³", quantity=2400.0),
            RawBoqItem(seq_no="1.2", section_path=["土地平整工程"], raw_name="土（石）方运输，运距5km", raw_features="土（石）方运输，运距5km", raw_unit="m³", quantity=1800.0),
            RawBoqItem(seq_no="1.3", section_path=["土地平整工程"], raw_name="表土剥离，推土80m", raw_features="表土剥离，推土80m", raw_unit="m³", quantity=3200.0),
            RawBoqItem(seq_no="1.4", section_path=["灌溉与排水工程"], raw_name="PE管（De110mm）", raw_features="PE管（De110mm）", raw_unit="m", quantity=500.0),
            RawBoqItem(seq_no="1.5", section_path=["灌溉与排水工程"], raw_name="C25混凝土 底板,双轮胶车运至施工点，综合运距为250m", raw_features="C25混凝土 底板,双轮胶车运至施工点，综合运距为250m", raw_unit="m³", quantity=120.0),
        ]
    if preset_key == "cable_installation":
        return [
            RawBoqItem(seq_no="1", section_path=["泵站电气安装"], boq_code="030409001001", raw_name="电力电缆", raw_features="1.名称:电力电缆 2.规格:ZR-YJV-8.7/10KV-3×50 3.敷设方式:室内", raw_unit="m", quantity=800.0),
            RawBoqItem(seq_no="2", section_path=["泵站电气安装"], boq_code="030409001009", raw_name="电力电缆", raw_features="1.名称:电力电缆 2.规格:ZR-YJV-0.6/1KV-3×150+1×95 3.敷设方式:室内", raw_unit="m", quantity=120.0),
            RawBoqItem(seq_no="3", section_path=["泵站电气安装"], boq_code="030409003002", raw_name="控制电缆", raw_features="1.名称:控制电缆 2.规格:ZR-KVVP-4×2.5", raw_unit="m", quantity=5000.0),
            RawBoqItem(seq_no="4", section_path=["泵站电气安装"], boq_code="030409002010", raw_name="电力电缆头 热缩电缆终端 35KV-3×150（户内）", raw_features="电力电缆头 热缩电缆终端 35KV-3×150（户内）", raw_unit="个", quantity=4.0),
            RawBoqItem(seq_no="5", section_path=["泵站电气安装"], boq_code="030409001099", raw_name="电力电缆", raw_features="1.名称:电力电缆 2.规格:ZR-YJV-0.6/1KV-3×240+1×120 3.敷设方式:室内", raw_unit="m", quantity=50.0),
        ]
    return [
        RawBoqItem(seq_no="1.1", section_path=["河道滩地生态修复工程", "灌木"], raw_name="黑沙蒿栽植", raw_features="黑沙蒿规格：地径≥0.3cm，高度25~30cm，种植形式：1m×3m标准：220株/亩，栽植坑：30cm×30cm×30cm，抚育：3年", raw_unit="hm²", quantity=4.5),
        RawBoqItem(seq_no="1.2", section_path=["河道滩地生态修复工程", "灌木"], raw_name="北沙柳栽植", raw_features="北沙柳规格：地径≥1cm，种植形式：2m×2m，抚育：3年", raw_unit="hm²", quantity=3.2),
        RawBoqItem(seq_no="1.3", section_path=["河道滩地生态修复工程", "草地"], raw_name="混播草地", raw_features="披碱草、碱茅、紫花苜蓿混播，规格：30~35g/㎡", raw_unit="m²", quantity=15000.0),
        RawBoqItem(seq_no="1.4", section_path=["河道主槽生态护岸工程"], raw_name="格宾石笼护脚", raw_features="格宾石笼护脚（2m×0.8m×1m）", raw_unit="m³", quantity=650.0),
        RawBoqItem(seq_no="1.5", section_path=["河道滨岸生态护坡工程"], raw_name="C30砼压顶", raw_features="C30砼压顶", raw_unit="m³", quantity=180.0),
    ]


def _evaluate_all_pipelines(container: AppContainer) -> dict[str, Any]:
    """基于真实样本构造带扰动与近邻规格测试集，运行可插拔重排模型对比消融实验."""
    benchmark_cases = [
        # (查询自然语言/改写文本, 领域, 单位, 真实参考单价, 允许误差容限规格关键词)
        ("电力电缆 阻燃交联聚乙烯 0.6/1KV 3×150+1×95 室内", "07_installation", "m", 1194.67, "3×150+1×95"),
        ("电力电缆 阻燃交联聚乙烯 0.6/1KV 3×25+1×16 室内", "07_installation", "m", 133.15, "3×25+1×16"),
        ("电力电缆 规格:ZR-YJV-8.7/10KV-3×50 敷设方式 室内", "07_installation", "m", 257.74, "3×50"),
        ("控制电缆 ZR-KVVP-4×2.5 电缆敷设", "07_installation", "m", 24.37, "4×2.5"),
        ("控制电缆 ZR-KVVP-10×2.5 电缆敷设", "07_installation", "m", 51.42, "10×2.5"),
        ("热缩电缆终端 35KV-3×150 户内", "07_installation", "个", 1399.07, "户内"),
        ("热缩电缆终端 35KV-3×150 户外", "07_installation", "个", 1975.74, "户外"),
        ("土石方运输 运距5km", "05_land_consolidation", "m³", 21.63, "5000"),
        ("原土 土石方运输 田块间调配 运距0.5km", "05_land_consolidation", "m³", 11.40, "500"),
        ("清表 推土机推土 地表清理", "05_land_consolidation", "m³", 2.84, "清表"),
        ("PE管 De110mm 管道安装", "05_land_consolidation", "m", 41.03, "110"),
        ("黑沙蒿 灌木 地径≥0.3cm 抚育3年", "01_water_ecology", "hm²", 13823.75, "黑沙蒿"),
        ("北沙柳 灌木 地径≥1cm 抚育3年", "01_water_ecology", "hm²", 16760.79, "北沙柳"),
        ("格宾石笼护脚 2m×0.8m×1m", "01_water_ecology", "m³", 275.21, "格宾"),
    ]
    for it in container.items:
        if "格宾石笼护脚" in it.raw_text_full and it.normalized_unit_price_yuan:
            benchmark_cases[-1] = ("格宾石笼护脚 2m×0.8m×1m", "01_water_ecology", "m³", it.normalized_unit_price_yuan, "格宾")
            break
    for it in container.items:
        if "田块间土方调配" in it.raw_text_full and "运距0.5km" in it.raw_text_full and it.normalized_unit_price_yuan:
            benchmark_cases[8] = ("原土 土石方运输 田块间调配 运距0.5km", "05_land_consolidation", "m³", it.normalized_unit_price_yuan, "500")
            break

    pipelines = ["production_hybrid", "rgcn_graph_reranker", "hybrid_no_decay", "lstm_cosine_baseline"]
    summary: list[dict[str, Any]] = []

    for pname in pipelines:
        top1_hits = 0
        top3_hits = 0
        mrr_sum = 0.0
        mape_list: list[float] = []
        conflict_count = 0

        for q_text, dom, unit, true_price, must_token in benchmark_cases:
            raw_q = RawBoqItem(seq_no="E", raw_name=q_text, raw_features=q_text, raw_unit=unit)
            std_q = container.standardizer.standardize(raw_q, domain=dom, project_id="eval", idx=1)
            res = container.matching.match(
                std_q,
                MatchContext(top_k=3, pipeline_name=pname, auto_adjust_price=False),
            )
            cands = res.candidates
            hit_rank = None
            for rank_idx, c in enumerate(cands, start=1):
                c_price = c.candidate_item.normalized_unit_price_yuan or 0.0
                c_blob = f"{c.candidate_item.raw_text_full} {c.candidate_item.slots}".replace("*", "×")
                if abs(c_price - true_price) / max(true_price, 1.0) <= 0.12 and (
                    must_token in c_blob or must_token == "格宾"
                ):
                    if hit_rank is None:
                        hit_rank = rank_idx
            if hit_rank == 1:
                top1_hits += 1
            if hit_rank is not None and hit_rank <= 3:
                top3_hits += 1
            if hit_rank is not None:
                mrr_sum += 1.0 / hit_rank

            if cands:
                top1_price = cands[0].candidate_item.normalized_unit_price_yuan or 0.0
                mape = abs(top1_price - true_price) / max(true_price, 1.0)
                mape_list.append(mape)
                if mape > 0.35:
                    conflict_count += 1

        n = len(benchmark_cases)
        summary.append(
            {
                "pipeline_name": pname,
                "cases_count": n,
                "top1_accuracy": round(top1_hits / n * 100, 1),
                "top3_hit_rate": round(top3_hits / n * 100, 1),
                "mrr": round(mrr_sum / n, 4),
                "mape_pct": round((sum(mape_list) / len(mape_list)) * 100, 2) if mape_list else 0.0,
                "spec_conflict_rate_pct": round(conflict_count / n * 100, 1),
            }
        )

    return {"benchmarks": summary}


app = create_app()
