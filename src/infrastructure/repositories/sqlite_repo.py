from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Optional

from src.core.contracts.boq_item import (
    CanonicalProject,
    FeatureSlotValue,
    StandardizedBoqItem,
    UnitPriceBreakdown,
)
from src.core.contracts.matching import AuditFinding
from src.core.contracts.project import (
    CostStage,
    EngineeringDomain,
    FeeRateRule,
    MaterialPriceRecord,
    ProjectContext,
    ProjectFileBundle,
)


class SqliteBoqRepository:
    """双库持久化仓储适配器（工程价格库 + 规则知识图谱 + 三级校审回流日志）"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    project_name TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    province TEXT NOT NULL,
                    city TEXT,
                    district TEXT,
                    stage TEXT NOT NULL,
                    price_base_month TEXT NOT NULL,
                    quota_standards TEXT NOT NULL,
                    labor_rates TEXT,
                    utility_rates TEXT,
                    fee_rules TEXT,
                    total_investment_yuan REAL,
                    source_files TEXT,
                    quality_score REAL DEFAULT 1.0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS boq_items (
                    item_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                    seq_no TEXT,
                    section_path TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    category_code TEXT NOT NULL,
                    standard_name TEXT NOT NULL,
                    boq_code TEXT,
                    raw_text_full TEXT NOT NULL,
                    canonical_search_text TEXT NOT NULL,
                    standard_unit TEXT,
                    raw_unit TEXT,
                    unit_conversion_factor REAL DEFAULT 1.0,
                    normalized_quantity REAL,
                    normalized_unit_price_yuan REAL,
                    raw_unit_price_yuan REAL,
                    total_price_yuan REAL,
                    slots_json TEXT NOT NULL,
                    has_l2_breakdown INTEGER DEFAULT 0,
                    data_source TEXT DEFAULT 'historical_import'
                );

                CREATE INDEX IF NOT EXISTS idx_boq_domain_cat ON boq_items(domain, category_code);
                CREATE INDEX IF NOT EXISTS idx_boq_std_name ON boq_items(standard_name);

                CREATE TABLE IF NOT EXISTS boq_price_breakdowns (
                    breakdown_id TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL REFERENCES boq_items(item_id) ON DELETE CASCADE,
                    quota_codes TEXT NOT NULL,
                    quota_unit TEXT,
                    breakdown_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS material_prices (
                    mat_price_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                    material_code TEXT,
                    standard_material_name TEXT NOT NULL,
                    spec_model TEXT,
                    unit TEXT NOT NULL,
                    base_price_yuan REAL,
                    budget_price_excl_tax REAL,
                    province TEXT,
                    district TEXT,
                    price_month TEXT
                );

                CREATE TABLE IF NOT EXISTS review_feedback_logs (
                    feedback_id TEXT PRIMARY KEY,
                    target_project_id TEXT NOT NULL,
                    query_text TEXT NOT NULL,
                    matched_item_id TEXT,
                    ai_recommended_price REAL,
                    final_approved_price REAL NOT NULL,
                    modify_reason TEXT,
                    reviewer_role TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS kg_triples (
                    triple_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                    head_type TEXT NOT NULL,
                    head_name TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    tail_type TEXT NOT NULL,
                    tail_name TEXT NOT NULL,
                    attributes_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS estimation_sessions (
                    session_id TEXT PRIMARY KEY,
                    project_name TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    province TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    price_base_month TEXT NOT NULL,
                    total_yuan REAL DEFAULT 0.0,
                    items_count INTEGER DEFAULT 0,
                    low_conf_count INTEGER DEFAULT 0,
                    audit_count INTEGER DEFAULT 0,
                    canonical_json TEXT NOT NULL,
                    rows_json TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def save_project(self, project: CanonicalProject, data_source: str = "historical_import") -> None:
        ctx = project.context
        with self._connect() as conn:
            conn.execute("DELETE FROM projects WHERE project_id = ?", (ctx.project_id,))
            conn.execute(
                """
                INSERT INTO projects (
                    project_id, project_name, domain, province, city, district, stage,
                    price_base_month, quota_standards, labor_rates, utility_rates, fee_rules,
                    total_investment_yuan, source_files, quality_score, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ctx.project_id,
                    ctx.project_name,
                    ctx.domain.value,
                    ctx.province,
                    ctx.city,
                    ctx.district,
                    ctx.stage.value,
                    ctx.price_base_month,
                    json.dumps(ctx.quota_standards, ensure_ascii=False),
                    json.dumps(ctx.labor_rates, ensure_ascii=False),
                    json.dumps(ctx.utility_rates, ensure_ascii=False),
                    json.dumps([r.model_dump() for r in ctx.fee_rules], ensure_ascii=False),
                    ctx.total_investment_yuan,
                    json.dumps(ctx.source_files, ensure_ascii=False),
                    project.quality_metrics.get("overall", 1.0),
                    ctx.created_at.isoformat(),
                ),
            )

            for it in project.items:
                slots_payload = {k: v.model_dump() for k, v in it.slots.items()}
                conn.execute(
                    """
                    INSERT INTO boq_items (
                        item_id, project_id, seq_no, section_path, domain, category_code,
                        standard_name, boq_code, raw_text_full, canonical_search_text,
                        standard_unit, raw_unit, unit_conversion_factor, normalized_quantity,
                        normalized_unit_price_yuan, raw_unit_price_yuan, total_price_yuan,
                        slots_json, has_l2_breakdown, data_source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        it.item_id,
                        ctx.project_id,
                        it.seq_no,
                        json.dumps(it.section_path, ensure_ascii=False),
                        it.domain,
                        it.category_code,
                        it.standard_name,
                        it.boq_code,
                        it.raw_text_full,
                        it.canonical_search_text,
                        it.standard_unit,
                        it.raw_unit,
                        it.unit_conversion_factor,
                        it.normalized_quantity,
                        it.normalized_unit_price_yuan,
                        it.raw_unit_price_yuan,
                        it.total_price_yuan,
                        json.dumps(slots_payload, ensure_ascii=False),
                        1 if it.breakdown else 0,
                        data_source,
                    ),
                )
                if it.breakdown:
                    bd_id = "bd_" + hashlib.md5(it.item_id.encode("utf-8")).hexdigest()[:12]
                    conn.execute(
                        """
                        INSERT INTO boq_price_breakdowns (
                            breakdown_id, item_id, quota_codes, quota_unit, breakdown_json
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            bd_id,
                            it.item_id,
                            json.dumps(it.breakdown.quota_codes, ensure_ascii=False),
                            it.breakdown.quota_unit,
                            it.breakdown.model_dump_json(),
                        ),
                    )

            for idx, mat in enumerate(ctx.base_materials):
                mid = "mat_" + hashlib.md5(
                    f"{ctx.project_id}:{idx}:{mat.material_name}:{mat.spec_model or ''}".encode("utf-8")
                ).hexdigest()[:12]
                conn.execute(
                    """
                    INSERT INTO material_prices (
                        mat_price_id, project_id, material_code, standard_material_name,
                        spec_model, unit, base_price_yuan, budget_price_excl_tax,
                        province, district, price_month
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mid,
                        ctx.project_id,
                        mat.material_code,
                        mat.material_name,
                        mat.spec_model,
                        mat.unit,
                        mat.base_price,
                        mat.budget_price_excl_tax,
                        ctx.province,
                        ctx.district,
                        mat.price_month or ctx.price_base_month,
                    ),
                )

            self._save_kg_triples(conn, project)

    def _save_kg_triples(self, conn: sqlite3.Connection, project: CanonicalProject) -> None:
        ctx = project.context
        triples: list[tuple[str, str, str, str, str, dict[str, Any]]] = []

        for q in ctx.quota_standards[:5]:
            triples.append(
                (
                    "EngineeringDomain",
                    ctx.domain.value,
                    "APPLIES_STANDARD",
                    "QuotaStandard",
                    q,
                    {"province": ctx.province, "stage": ctx.stage.value},
                )
            )
        for rule in ctx.fee_rules:
            triples.append(
                (
                    "QuotaStandard",
                    ctx.quota_standards[0] if ctx.quota_standards else ctx.project_name,
                    "HAS_FEE_RATE",
                    "SectionCategory",
                    rule.engineering_category,
                    {
                        "indirect_rate_pct": rule.indirect_fee_rate,
                        "indirect_base": rule.indirect_fee_base,
                        "temp_facility_rate": rule.temp_facility_rate,
                    },
                )
            )

        seen_quota_pairs = set()
        for it in project.items:
            if it.breakdown and it.breakdown.quota_codes:
                q_str = "+".join(it.breakdown.quota_codes)
                pair_key = (it.standard_name, q_str)
                if pair_key not in seen_quota_pairs and len(seen_quota_pairs) < 40:
                    seen_quota_pairs.add(pair_key)
                    triples.append(
                        (
                            "BoqTemplate",
                            it.standard_name,
                            "MAPS_TO_QUOTA",
                            "QuotaSubItem",
                            q_str,
                            {"unit": it.breakdown.quota_unit, "sample_item": it.raw_text_full[:40]},
                        )
                    )
                    for line in it.breakdown.resource_lines:
                        if line.resource_type in ("材料", "价差", "机械") and line.quantity > 0:
                            triples.append(
                                (
                                    "QuotaSubItem",
                                    q_str,
                                    "REQUIRES_RESOURCE",
                                    "Resource",
                                    line.name,
                                    {"qty": line.quantity, "unit": line.unit},
                                )
                            )

        for h_t, h_n, rel, t_t, t_n, attrs in triples:
            tid = "kg_" + hashlib.md5(f"{ctx.project_id}:{h_n}:{rel}:{t_n}".encode("utf-8")).hexdigest()[:12]
            conn.execute(
                """
                INSERT OR IGNORE INTO kg_triples (
                    triple_id, project_id, head_type, head_name, relation, tail_type, tail_name, attributes_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (tid, ctx.project_id, h_t, h_n, rel, t_t, t_n, json.dumps(attrs, ensure_ascii=False)),
            )

    def load_all_projects_and_items(self) -> tuple[list[ProjectContext], list[StandardizedBoqItem]]:
        with self._connect() as conn:
            proj_rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
            mat_rows = conn.execute("SELECT * FROM material_prices").fetchall()
            bd_rows = conn.execute("SELECT * FROM boq_price_breakdowns").fetchall()
            item_rows = conn.execute("SELECT * FROM boq_items").fetchall()

        mats_by_proj: dict[str, list[MaterialPriceRecord]] = {}
        for mr in mat_rows:
            mats_by_proj.setdefault(mr["project_id"], []).append(
                MaterialPriceRecord(
                    material_code=mr["material_code"],
                    material_name=mr["standard_material_name"],
                    spec_model=mr["spec_model"],
                    unit=mr["unit"],
                    base_price=mr["base_price_yuan"],
                    budget_price_excl_tax=mr["budget_price_excl_tax"],
                    region=mr["district"] or mr["province"],
                    price_month=mr["price_month"],
                )
            )

        bd_by_item: dict[str, UnitPriceBreakdown] = {}
        for br in bd_rows:
            bd_by_item[br["item_id"]] = UnitPriceBreakdown.model_validate_json(br["breakdown_json"])

        projects: list[ProjectContext] = []
        for pr in proj_rows:
            projects.append(
                ProjectContext(
                    project_id=pr["project_id"],
                    project_name=pr["project_name"],
                    domain=EngineeringDomain(pr["domain"]),
                    province=pr["province"],
                    city=pr["city"],
                    district=pr["district"],
                    stage=CostStage(pr["stage"]),
                    price_base_month=pr["price_base_month"],
                    quota_standards=json.loads(pr["quota_standards"] or "[]"),
                    labor_rates=json.loads(pr["labor_rates"] or "{}"),
                    utility_rates=json.loads(pr["utility_rates"] or "{}"),
                    fee_rules=[FeeRateRule(**r) for r in json.loads(pr["fee_rules"] or "[]")],
                    base_materials=mats_by_proj.get(pr["project_id"], []),
                    total_investment_yuan=pr["total_investment_yuan"],
                    source_files=json.loads(pr["source_files"] or "[]"),
                )
            )

        items: list[StandardizedBoqItem] = []
        for ir in item_rows:
            slots_raw = json.loads(ir["slots_json"] or "{}")
            slots = {k: FeatureSlotValue(**v) for k, v in slots_raw.items()}
            items.append(
                StandardizedBoqItem(
                    item_id=ir["item_id"],
                    project_id=ir["project_id"],
                    seq_no=ir["seq_no"] or "",
                    domain=ir["domain"],
                    category_code=ir["category_code"],
                    standard_name=ir["standard_name"],
                    boq_code=ir["boq_code"],
                    section_path=json.loads(ir["section_path"] or "[]"),
                    raw_text_full=ir["raw_text_full"],
                    canonical_search_text=ir["canonical_search_text"],
                    standard_unit=ir["standard_unit"],
                    raw_unit=ir["raw_unit"],
                    unit_conversion_factor=ir["unit_conversion_factor"] or 1.0,
                    normalized_quantity=ir["normalized_quantity"],
                    normalized_unit_price_yuan=ir["normalized_unit_price_yuan"],
                    raw_unit_price_yuan=ir["raw_unit_price_yuan"],
                    total_price_yuan=ir["total_price_yuan"],
                    slots=slots,
                    breakdown=bd_by_item.get(ir["item_id"]),
                )
            )
        return projects, items

    def list_kg_triples(self, limit: int = 120) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT k.*, p.project_name
                FROM kg_triples k
                JOIN projects p ON k.project_id = p.project_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "triple_id": r["triple_id"],
                "project_name": r["project_name"],
                "head_type": r["head_type"],
                "head_name": r["head_name"],
                "relation": r["relation"],
                "tail_type": r["tail_type"],
                "tail_name": r["tail_name"],
                "attributes": json.loads(r["attributes_json"]),
            }
            for r in rows
        ]

    def save_review_feedback(
        self,
        target_project_id: str,
        query_item: StandardizedBoqItem,
        matched_item_id: Optional[str],
        ai_price: Optional[float],
        final_price: float,
        modify_reason: str,
        reviewer_role: str = "校核人",
        persist_to_db: bool = True,
    ) -> str:
        fid = "fb_" + hashlib.md5(f"{target_project_id}:{query_item.item_id}:{final_price}".encode("utf-8")).hexdigest()[:12]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO review_feedback_logs (
                    feedback_id, target_project_id, query_text, matched_item_id,
                    ai_recommended_price, final_approved_price, modify_reason, reviewer_role
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fid,
                    target_project_id,
                    query_item.raw_text_full,
                    matched_item_id,
                    ai_price,
                    final_price,
                    modify_reason,
                    reviewer_role,
                ),
            )
            if persist_to_db:
                # 保证回流父项目存在
                fb_proj_id = "proj_review_feedback"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO projects (
                        project_id, project_name, domain, province, stage,
                        price_base_month, quota_standards, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        fb_proj_id,
                        "三级校审定稿回流库",
                        query_item.domain,
                        "多省定稿回流",
                        CostStage.CONSTRUCTION_DRAWING.value,
                        "2026-11",
                        json.dumps(["三级校审定稿沉淀"], ensure_ascii=False),
                    ),
                )
                new_item_id = f"boq_fb_{fid}"
                slots_payload = {k: v.model_dump() for k, v in query_item.slots.items()}
                conn.execute(
                    """
                    INSERT OR REPLACE INTO boq_items (
                        item_id, project_id, seq_no, section_path, domain, category_code,
                        standard_name, boq_code, raw_text_full, canonical_search_text,
                        standard_unit, raw_unit, unit_conversion_factor, normalized_quantity,
                        normalized_unit_price_yuan, raw_unit_price_yuan, total_price_yuan,
                        slots_json, has_l2_breakdown, data_source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'review_feedback')
                    """,
                    (
                        new_item_id,
                        fb_proj_id,
                        query_item.seq_no,
                        json.dumps(query_item.section_path, ensure_ascii=False),
                        query_item.domain,
                        query_item.category_code,
                        query_item.standard_name,
                        query_item.boq_code,
                        query_item.raw_text_full,
                        query_item.canonical_search_text,
                        query_item.standard_unit,
                        query_item.raw_unit,
                        query_item.unit_conversion_factor,
                        query_item.normalized_quantity,
                        final_price,
                        final_price,
                        round((query_item.normalized_quantity or 1.0) * final_price, 2),
                        json.dumps(slots_payload, ensure_ascii=False),
                    ),
                )
        return fid

    def get_stats_summary(self) -> dict[str, Any]:
        with self._connect() as conn:
            p_cnt = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            i_cnt = conn.execute("SELECT COUNT(*) FROM boq_items").fetchone()[0]
            bd_cnt = conn.execute("SELECT COUNT(*) FROM boq_price_breakdowns").fetchone()[0]
            m_cnt = conn.execute("SELECT COUNT(*) FROM material_prices").fetchone()[0]
            kg_cnt = conn.execute("SELECT COUNT(*) FROM kg_triples").fetchone()[0]
            fb_cnt = conn.execute("SELECT COUNT(*) FROM review_feedback_logs").fetchone()[0]
            s_cnt = conn.execute("SELECT COUNT(*) FROM estimation_sessions").fetchone()[0]
        return {
            "projects_count": p_cnt,
            "boq_items_count": i_cnt,
            "l2_breakdowns_count": bd_cnt,
            "materials_count": m_cnt,
            "kg_triples_count": kg_cnt,
            "feedback_logs_count": fb_cnt,
            "sessions_count": s_cnt,
        }

    def save_estimation_session(
        self,
        session_id: str,
        canonical: CanonicalProject,
        rows: list[dict[str, Any]],
        details_map: dict[str, dict[str, Any]],
    ) -> None:
        ctx = canonical.context
        total_yuan = round(sum(it.total_price_yuan or 0.0 for it in canonical.items), 2)
        items_count = len(canonical.items)
        low_conf_count = sum(1 for d in details_map.values() if d.get("confidence") == "low")
        audit_count = len(canonical.audit_findings)
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO estimation_sessions (
                    session_id, project_name, domain, province, stage, price_base_month,
                    total_yuan, items_count, low_conf_count, audit_count,
                    canonical_json, rows_json, details_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    ctx.project_name,
                    ctx.domain.value,
                    ctx.province,
                    ctx.stage.value,
                    ctx.price_base_month,
                    total_yuan,
                    items_count,
                    low_conf_count,
                    audit_count,
                    canonical.model_dump_json(),
                    json.dumps(rows, ensure_ascii=False),
                    json.dumps(details_map, ensure_ascii=False),
                    updated_at,
                ),
            )

    def load_estimation_session(self, session_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM estimation_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if not row:
            return None
        canonical = CanonicalProject.model_validate_json(row["canonical_json"])
        canonical.audit_findings = [
            f if isinstance(f, AuditFinding) else AuditFinding(**f)
            for f in canonical.audit_findings
        ]
        return {
            "session_id": row["session_id"],
            "project": canonical,
            "rows": json.loads(row["rows_json"]),
            "details_map": json.loads(row["details_json"]),
            "total_yuan": row["total_yuan"],
            "updated_at": row["updated_at"],
        }

    def list_estimation_sessions(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT session_id, project_name, domain, province, stage, price_base_month,
                       total_yuan, items_count, low_conf_count, audit_count, updated_at
                FROM estimation_sessions
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_estimation_session(self, session_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM estimation_sessions WHERE session_id = ?", (session_id,))
        return cur.rowcount > 0

    def bootstrap_sample_projects(self, data_dir: Path, ingestion_module: Any) -> int:
        """若库内尚无工程项目，自动解析导入工作区 data/ 下的 3 套真实历史工程项目包."""
        if self.get_stats_summary()["projects_count"] > 0:
            return 0

        bundles: list[ProjectFileBundle] = []

        # 1. 西柳沟流域生态综合治理工程
        f1_doc = data_dir / "西柳沟流域生态综合治理工程初步设计概算09.docx"
        f1_xls = data_dir / "西柳沟流域生态综合治理工程10.19单行本导表.xls"
        if f1_doc.exists() and f1_xls.exists():
            bundles.append(
                ProjectFileBundle(
                    project_name="西柳沟流域生态综合治理工程",
                    domain_hint=EngineeringDomain.WATER_ECOLOGY,
                    files=[f1_doc, f1_xls],
                )
            )

        # 2. 高明区明城镇全域土地综合整治项目（第四批次）
        f2_doc = data_dir / "高明区明城镇全域土地综合整治项目（一标段.doc"
        f2_xls = data_dir / "高明区明城镇全域土地综合整治项目第四批次.xls"
        if f2_doc.exists() and f2_xls.exists():
            bundles.append(
                ProjectFileBundle(
                    project_name="高明区明城镇全域土地综合整治项目（第四批次）",
                    domain_hint=EngineeringDomain.LAND_CONSOLIDATION,
                    files=[f2_doc, f2_xls],
                )
            )

        # 3. 二级泵站电缆工程
        f3_pdf = data_dir / "二级泵站电缆综合单价.pdf"
        if f3_pdf.exists():
            bundles.append(
                ProjectFileBundle(
                    project_name="二级泵站电缆",
                    domain_hint=EngineeringDomain.INSTALLATION_GENERAL,
                    files=[f3_pdf],
                )
            )

        imported = 0
        for bundle in bundles:
            proj = ingestion_module.ingest(bundle)
            self.save_project(proj)
            imported += 1

        # 当公开仓库克隆后本地尚无私有 data/ 工程文件时，自动注入脱敏演示数据集以保证开箱即用
        if imported == 0:
            imported += self._seed_public_demo_projects(ingestion_module)
        return imported

    def _seed_public_demo_projects(self, ingestion_module: Any) -> int:
        from src.core.contracts.boq_item import QuotaResourceLine, RawBoqItem, UnitPriceBreakdown

        std_mod = getattr(ingestion_module, "_standardizer", None)
        if std_mod is None:
            from src.modules.standardizer.service import SemanticStandardizerModule

            std_mod = SemanticStandardizerModule()

        demo_specs = [
            (
                ProjectContext(
                    project_id="demo_water_01",
                    project_name="示范生态河道综合治理工程（脱敏演示）",
                    domain=EngineeringDomain.WATER_ECOLOGY,
                    province="内蒙古自治区",
                    stage=CostStage.PRELIMINARY_DESIGN,
                    price_base_month="2026-07",
                    quota_standards=["水总〔2024〕323号《水利工程设计概（估）算编制规定》"],
                    base_materials=[MaterialPriceRecord(material_name="柴油", unit="t", base_price=2500.0, budget_price_excl_tax=7400.0)],
                ),
                [
                    RawBoqItem(seq_no="1.1.1", section_path=["河道生态修复", "灌木"], raw_name="黑沙蒿栽植", raw_features="黑沙蒿规格：地径≥0.3cm，高度25~30cm，种植形式：1m×3m标准：220株/亩，栽植坑：30cm×30cm×30cm，抚育：3年", raw_unit="hm²", quantity=3.5, unit_price_yuan=13800.0, total_price_yuan=48300.0),
                    RawBoqItem(seq_no="1.1.2", section_path=["河道生态修复", "灌木"], raw_name="北沙柳栽植", raw_features="北沙柳规格：地径≥1cm，种植形式：2m×2m，抚育：3年", raw_unit="hm²", quantity=2.8, unit_price_yuan=16750.0, total_price_yuan=46900.0),
                    RawBoqItem(seq_no="1.2.1", section_path=["河道主槽生态护岸"], raw_name="格宾石笼护脚", raw_features="格宾石笼护脚（2m×0.8m×1m）", raw_unit="m³", quantity=420.0, unit_price_yuan=275.0, total_price_yuan=115500.0),
                ],
            ),
            (
                ProjectContext(
                    project_id="demo_land_02",
                    project_name="示范全域土地综合整治工程（脱敏演示）",
                    domain=EngineeringDomain.LAND_CONSOLIDATION,
                    province="广东省",
                    stage=CostStage.CONSTRUCTION_DRAWING,
                    price_base_month="2025-07",
                    quota_standards=["《土地开发整理项目预算定额标准》（财综〔2021〕128号）"],
                    base_materials=[MaterialPriceRecord(material_name="柴油", unit="t", base_price=4500.0, budget_price_excl_tax=7000.0)],
                ),
                [
                    RawBoqItem(seq_no="1.1.1", section_path=["土地平整工程"], raw_name="清表，推土机推土", raw_features="清表，推土机推土", raw_unit="m³", quantity=1800.0, unit_price_yuan=2.84, total_price_yuan=5112.0),
                    RawBoqItem(
                        seq_no="1.1.2",
                        section_path=["土地平整工程"],
                        raw_name="土（石）方运输，运距5km",
                        raw_features="土（石）方运输，运距5km",
                        raw_unit="m³",
                        quantity=1500.0,
                        unit_price_yuan=21.63,
                        total_price_yuan=32445.0,
                        breakdown=UnitPriceBreakdown(
                            quota_codes=["10224"],
                            quota_unit="100m³",
                            tax_rate_pct=9.0,
                            resource_lines=[QuotaResourceLine(resource_type="价差", name="柴油", unit="kg", quantity=127.56, unit_price=2.5, subtotal=318.89)],
                        ),
                    ),
                    RawBoqItem(seq_no="1.2.1", section_path=["灌溉与排水工程"], raw_name="PE管（De110mm）", raw_features="PE管（De110mm）", raw_unit="m", quantity=360.0, unit_price_yuan=41.03, total_price_yuan=14770.8),
                ],
            ),
            (
                ProjectContext(
                    project_id="demo_cable_03",
                    project_name="示范泵站电气安装工程（脱敏演示）",
                    domain=EngineeringDomain.INSTALLATION_GENERAL,
                    province="未指定",
                    stage=CostStage.BID_CONTROL,
                    price_base_month="2026-01",
                    quota_standards=["GB50500-2024 建设工程工程量清单计价标准"],
                ),
                [
                    RawBoqItem(seq_no="1", section_path=["通用安装工程"], boq_code="030409001009", raw_name="电力电缆", raw_features="1.名称:电力电缆 2.规格:ZR-YJV-0.6/1KV-3×150+1×95 3.敷设方式:室内", raw_unit="m", quantity=66.0, unit_price_yuan=1194.67, total_price_yuan=78848.22),
                    RawBoqItem(seq_no="2", section_path=["通用安装工程"], boq_code="030409001010", raw_name="电力电缆", raw_features="1.名称:电力电缆 2.规格:ZR-YJV-0.6/1KV-3×25+1×16 3.敷设方式:室内", raw_unit="m", quantity=341.0, unit_price_yuan=133.15, total_price_yuan=45404.15),
                    RawBoqItem(seq_no="3", section_path=["通用安装工程"], boq_code="030409003002", raw_name="控制电缆", raw_features="1.名称:控制电缆 2.规格:ZR-KVVP-4×2.5", raw_unit="m", quantity=1200.0, unit_price_yuan=24.37, total_price_yuan=29244.0),
                ],
            ),
        ]

        count = 0
        for p_ctx, r_items in demo_specs:
            std_items = [
                std_mod.standardize(r, domain=p_ctx.domain.value, project_id=p_ctx.project_id, idx=idx + 1)
                for idx, r in enumerate(r_items)
            ]
            self.save_project(CanonicalProject(context=p_ctx, items=std_items, raw_items_count=len(std_items)))
            count += 1
        return count
