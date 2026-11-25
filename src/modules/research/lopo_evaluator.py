from __future__ import annotations

import hashlib
import math
import time
from typing import Any, Optional

import numpy as np

from src.core.contracts.boq_item import StandardizedBoqItem
from src.core.contracts.matching import ConfidenceLevel, MatchContext
from src.core.contracts.project import ProjectContext
from src.modules.matching.rerankers.hybrid_kernel_reranker import slot_similarity
from src.modules.matching.service import PriceMatchingModule
from src.modules.pricing.service import PriceAdjustmentModule
from src.modules.research.rgcn_trainer import train_rgcn_model
from src.modules.standardizer.unit_converter import units_compatible


def _is_oracle_match(q: StandardizedBoqItem, c: StandardizedBoqItem) -> bool:
    if q.item_id == c.item_id:
        return False
    if not units_compatible(q.standard_unit, c.standard_unit):
        return False
    qp = q.normalized_unit_price_yuan
    cp = c.normalized_unit_price_yuan
    if qp is None or cp is None or qp <= 0 or cp <= 0:
        return False
    if abs(cp - qp) / max(qp, 1.0) > 0.15:
        return False
    if q.domain != c.domain and q.category_code != c.category_code:
        return False
    shared = set(q.slots.keys()) & set(c.slots.keys())
    for k in shared:
        if slot_similarity(q.slots[k], c.slots[k])[1]:
            return False
    return (q.category_code == c.category_code) or (q.standard_name == c.standard_name)


def _paired_bootstrap_pvalue(
    scores_a: list[float],
    scores_b: list[float],
    n_boot: int = 1000,
    seed: int = 42,
) -> dict[str, Any]:
    """双侧配对 Bootstrap 重采样显著性检验，比较模型 A vs 基线 B."""
    n = min(len(scores_a), len(scores_b))
    if n == 0:
        return {"mean_diff": 0.0, "ci95_low": 0.0, "ci95_high": 0.0, "p_value": 1.0, "significant": False}
    a = np.asarray(scores_a[:n], dtype=np.float64)
    b = np.asarray(scores_b[:n], dtype=np.float64)
    obs_diff = float(np.mean(a - b))

    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_boot, n))
    diffs = np.mean(a[idx] - b[idx], axis=1)
    ci_low = float(np.percentile(diffs, 2.5))
    ci_high = float(np.percentile(diffs, 97.5))

    # 在零假设（无差异）中心化后计算双侧经验 p-value
    centered = diffs - obs_diff
    p_val = float((np.sum(np.abs(centered) >= abs(obs_diff)) + 1.0) / (n_boot + 1.0))
    return {
        "mean_diff": round(obs_diff, 4),
        "ci95_low": round(ci_low, 4),
        "ci95_high": round(ci_high, 4),
        "p_value": round(p_val, 4),
        "significant_005": p_val < 0.05,
        "significant_001": p_val < 0.01,
    }


class LopoCrossValidator:
    """零数据泄露留一子项目交叉验证引擎 (Leave-One-Subproject-Out Cross-Validation)."""

    def __init__(
        self,
        items: list[StandardizedBoqItem],
        projects: list[ProjectContext],
        repo: Optional[Any] = None,
    ) -> None:
        self.items = list(items)
        self.projects = list(projects)
        self.repo = repo

    def build_folds(self, max_eval_per_fold: int = 24) -> list[dict[str, Any]]:
        folds: list[dict[str, Any]] = []

        # Fold 1 & 2: 高明区土地整治 跨施工片区留一互相外推 (光明东片区 <-> 新岗西片区)
        land_items = [it for it in self.items if it.domain == "05_land_consolidation"]
        east_ids = {it.item_id for it in land_items if it.section_path and it.section_path[0] == "光明东片区"}
        west_ids = {it.item_id for it in land_items if it.section_path and it.section_path[0] == "新岗西片区"}
        if east_ids and west_ids:
            folds.append(
                {
                    "fold_id": "fold_land_west_holdout",
                    "fold_name": "土地整治跨片区外推 (光明东 -> 新岗西)",
                    "holdout_ids": west_ids,
                    "max_queries": max_eval_per_fold,
                }
            )
            folds.append(
                {
                    "fold_id": "fold_land_east_holdout",
                    "fold_name": "土地整治跨片区外推 (新岗西 -> 光明东)",
                    "holdout_ids": east_ids,
                    "max_queries": max_eval_per_fold,
                }
            )

        # Fold 3: 水环境 西柳沟流域 按桩号/子工程分区构建互斥留出折
        water_items = [it for it in self.items if it.domain == "01_water_ecology" and (it.normalized_unit_price_yuan or 0) > 0]
        if len(water_items) >= 20:
            water_hold_a: set[str] = set()
            water_hold_b: set[str] = set()
            for it in water_items:
                sec_key = it.section_path[-1] if it.section_path else it.item_id
                bucket = int(hashlib.md5(sec_key.encode("utf-8")).hexdigest()[:4], 16) % 4
                if bucket == 0:
                    water_hold_a.add(it.item_id)
                elif bucket == 1:
                    water_hold_b.add(it.item_id)
            if water_hold_a:
                folds.append(
                    {
                        "fold_id": "fold_water_reaches_a",
                        "fold_name": "水环境流域跨标段留出 A 折",
                        "holdout_ids": water_hold_a,
                        "max_queries": max_eval_per_fold,
                    }
                )
            if water_hold_b:
                folds.append(
                    {
                        "fold_id": "fold_water_reaches_b",
                        "fold_name": "水环境流域跨标段留出 B 折",
                        "holdout_ids": water_hold_b,
                        "max_queries": max_eval_per_fold,
                    }
                )

        # Fold 4: 安装工程与 02/03/04/06 新兴领域 留一子集与异规格防错配外推折
        other_domains = ("02_urban_water", "03_irrigation", "04_coal_mine_water", "06_marine_ecology", "07_installation")
        other_items = [it for it in self.items if it.domain in other_domains and (it.normalized_unit_price_yuan or 0) > 0]
        if len(other_items) >= 12:
            strat_hold: set[str] = set()
            by_dom: dict[str, list[StandardizedBoqItem]] = {}
            for it in other_items:
                by_dom.setdefault(it.domain, []).append(it)
            for dom, grp in sorted(by_dom.items()):
                step = max(2, len(grp) // 4)
                for idx in range(0, len(grp), step):
                    strat_hold.add(grp[idx].item_id)
            folds.append(
                {
                    "fold_id": "fold_install_and_4domains",
                    "fold_name": "安装工程与四大新兴领域分层留出折",
                    "holdout_ids": strat_hold,
                    "max_queries": max_eval_per_fold + 8,
                }
            )

        return folds

    def evaluate(
        self,
        *,
        train_rgcn_per_fold: bool = True,
        rgcn_epochs: int = 25,
        pipelines: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """执行全流程零泄露 LOPO-CV，并对所有折内训练与验证计算 95% CI 与配对显著性 p 值."""
        t0 = time.perf_counter()
        folds_spec = self.build_folds()
        pipe_names = pipelines or [
            "production_hybrid",
            "rgcn_graph_reranker",
            "hybrid_no_decay",
            "lstm_cosine_baseline",
        ]

        # 按流水线收集逐样本二元观测值，供跨折微观汇总与 Bootstrap 检验
        per_sample_top1: dict[str, list[float]] = {p: [] for p in pipe_names}
        per_sample_top3: dict[str, list[float]] = {p: [] for p in pipe_names}
        per_sample_mrr: dict[str, list[float]] = {p: [] for p in pipe_names}
        per_sample_ndcg3: dict[str, list[float]] = {p: [] for p in pipe_names}
        per_sample_mape: dict[str, list[float]] = {p: [] for p in pipe_names}
        per_sample_conflict: dict[str, list[float]] = {p: [] for p in pipe_names}
        per_sample_is_support: list[bool] = []

        fold_reports: list[dict[str, Any]] = []
        learned_alphas: list[float] = []
        dense_backend_info: dict[str, Any] = {}

        for f_spec in folds_spec:
            holdout_ids: set[str] = f_spec["holdout_ids"]
            train_items = [it for it in self.items if it.item_id not in holdout_ids]
            hold_items = [it for it in self.items if it.item_id in holdout_ids and (it.normalized_unit_price_yuan or 0) > 0]

            # 严禁将 holdout_ids 放入索引，保持零信息泄露
            matcher = PriceMatchingModule(pricing_module=PriceAdjustmentModule())
            matcher.build_index(train_items, self.projects, repo=self.repo)

            dense_ret = matcher._retrievers.get("dense")
            if not dense_backend_info and dense_ret is not None and hasattr(dense_ret, "status_info"):
                dense_backend_info = dense_ret.status_info()

            # 在当前折 train_items 上独立重新训练 R-GCN（杜绝跨折信息泄露）
            if train_rgcn_per_fold and dense_ret is not None and hasattr(dense_ret, "export_doc_matrix"):
                ids_list, mat = dense_ret.export_doc_matrix()
                vec_map = {iid: mat[i] for i, iid in enumerate(ids_list)} if mat is not None else {}
                fold_pred = train_rgcn_model(
                    train_items,
                    self.projects,
                    vec_map,
                    epochs=rgcn_epochs,
                    save_path=None,
                    seed=42,
                )
                learned_alphas.append(fold_pred.learned_alpha)
                rgcn_rr = matcher._rerankers.get("rgcn_graph_reranker")
                if rgcn_rr is not None and hasattr(rgcn_rr, "set_predictor"):
                    rgcn_rr.set_predictor(fold_pred, dense_encoder=dense_ret)

            # 按 Oracle 是否存在于 train_items 划分同档泛化题 vs 缺档拒识题
            support_qs: list[StandardizedBoqItem] = []
            ood_qs: list[StandardizedBoqItem] = []
            for q in hold_items:
                if any(_is_oracle_match(q, tr) for tr in train_items):
                    support_qs.append(q)
                else:
                    ood_qs.append(q)

            max_q = int(f_spec.get("max_queries", 24))
            selected_support = support_qs[: max(1, int(max_q * 0.75))]
            selected_ood = ood_qs[: max(0, max_q - len(selected_support))]
            eval_queries = [(q, True) for q in selected_support] + [(q, False) for q in selected_ood]
            if not eval_queries:
                continue

            fold_pipe_stats: dict[str, Any] = {}
            for pname in pipe_names:
                f_top1 = 0
                f_top3 = 0
                f_mrr = 0.0
                f_mapes: list[float] = []
                f_conf = 0

                for q, is_support in eval_queries:
                    if pname == pipe_names[0]:
                        per_sample_is_support.append(is_support)
                    res = matcher.match(
                        q,
                        MatchContext(
                            top_k=3,
                            pipeline_name=pname,
                            auto_adjust_price=False,
                        ),
                    )
                    cands = res.candidates
                    if not is_support:
                        is_rejected = (res.confidence == ConfidenceLevel.LOW) or bool(
                            cands and cands[0].has_hard_spec_conflict
                        )
                        hit1 = 1.0 if is_rejected else 0.0
                        conf_flag = 0.0 if is_rejected else 1.0
                        per_sample_top1[pname].append(hit1)
                        per_sample_top3[pname].append(hit1)
                        per_sample_mrr[pname].append(hit1)
                        per_sample_ndcg3[pname].append(hit1)
                        per_sample_conflict[pname].append(conf_flag)
                        f_top1 += int(hit1)
                        f_top3 += int(hit1)
                        f_mrr += hit1
                        f_conf += int(conf_flag)
                        continue

                    hit_rank: Optional[int] = None
                    dcg = 0.0
                    for r_idx, c in enumerate(cands[:3], start=1):
                        if _is_oracle_match(q, c.candidate_item):
                            dcg += 1.0 / math.log2(r_idx + 1)
                            if hit_rank is None:
                                hit_rank = r_idx

                    hit1 = 1.0 if hit_rank == 1 else 0.0
                    hit3 = 1.0 if (hit_rank is not None and hit_rank <= 3) else 0.0
                    mrr_val = (1.0 / hit_rank) if hit_rank is not None else 0.0
                    ndcg_val = min(1.0, dcg)

                    true_p = float(q.normalized_unit_price_yuan or 1.0)
                    pred_p = float(cands[0].candidate_item.normalized_unit_price_yuan or 0.0) if cands else 0.0
                    mape_val = abs(pred_p - true_p) / max(true_p, 1.0) * 100.0
                    conf_flag = 1.0 if (not cands or mape_val > 35.0 or (cands and cands[0].has_hard_spec_conflict)) else 0.0

                    per_sample_top1[pname].append(hit1)
                    per_sample_top3[pname].append(hit3)
                    per_sample_mrr[pname].append(mrr_val)
                    per_sample_ndcg3[pname].append(ndcg_val)
                    per_sample_mape[pname].append(mape_val)
                    per_sample_conflict[pname].append(conf_flag)

                    f_top1 += int(hit1)
                    f_top3 += int(hit3)
                    f_mrr += mrr_val
                    f_mapes.append(mape_val)
                    f_conf += int(conf_flag)

                nq = len(eval_queries)
                fold_pipe_stats[pname] = {
                    "top1_accuracy": round(f_top1 / nq * 100, 1),
                    "top3_hit_rate": round(f_top3 / nq * 100, 1),
                    "mrr": round(f_mrr / nq, 4),
                    "mape_pct": round(float(np.mean(f_mapes)) if f_mapes else 0.0, 2),
                    "conflict_rate_pct": round(f_conf / nq * 100, 1),
                }

            fold_reports.append(
                {
                    "fold_id": f_spec["fold_id"],
                    "fold_name": f_spec["fold_name"],
                    "train_count": len(train_items),
                    "holdout_count": len(hold_items),
                    "support_queries": len(selected_support),
                    "ood_reject_queries": len(selected_ood),
                    "metrics": fold_pipe_stats,
                }
            )

        # 汇总跨折微观整体指标与与基线的显著性检验
        overall: list[dict[str, Any]] = []
        base_top1 = per_sample_top1.get("lstm_cosine_baseline", [])
        base_conf = per_sample_conflict.get("lstm_cosine_baseline", [])

        for pname in pipe_names:
            t1_arr = per_sample_top1[pname]
            n_tot = len(t1_arr) or 1
            sig_vs_base = (
                _paired_bootstrap_pvalue(t1_arr, base_top1)
                if pname != "lstm_cosine_baseline" and base_top1
                else None
            )
            sig_conf_vs_base = (
                _paired_bootstrap_pvalue(base_conf, per_sample_conflict[pname])
                if pname != "lstm_cosine_baseline" and base_conf
                else None
            )
            overall.append(
                {
                    "pipeline_name": pname,
                    "total_queries": len(t1_arr),
                    "support_queries": sum(1 for x in per_sample_is_support if x),
                    "ood_reject_queries": sum(1 for x in per_sample_is_support if not x),
                    "top1_accuracy": round(sum(t1_arr) / n_tot * 100, 1),
                    "top3_hit_rate": round(sum(per_sample_top3[pname]) / n_tot * 100, 1),
                    "mrr": round(sum(per_sample_mrr[pname]) / n_tot, 4),
                    "ndcg3": round(sum(per_sample_ndcg3[pname]) / n_tot, 4),
                    "mape_pct": round(float(np.mean(per_sample_mape[pname])) if per_sample_mape[pname] else 0.0, 2),
                    "spec_conflict_rate_pct": round(sum(per_sample_conflict[pname]) / n_tot * 100, 1),
                    "top1_vs_baseline": sig_vs_base,
                    "conflict_reduction_vs_baseline": sig_conf_vs_base,
                }
            )

        return {
            "protocol": "Zero-Leakage Leave-One-Subproject-Out Cross-Validation (LOPO-CV)",
            "folds_count": len(fold_reports),
            "elapsed_sec": round(time.perf_counter() - t0, 2),
            "dense_backend": dense_backend_info,
            "avg_learned_rgcn_alpha": round(float(np.mean(learned_alphas)), 3) if learned_alphas else None,
            "overall_summary": overall,
            "folds": fold_reports,
        }
