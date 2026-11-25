from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
from pathlib import Path
import random
import time
from typing import Any, Optional

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from src.core.contracts.boq_item import StandardizedBoqItem
from src.core.contracts.project import ProjectContext
from src.core.paths import get_runtime_data_dir
from src.modules.matching.rerankers.hybrid_kernel_reranker import slot_similarity
from src.modules.standardizer.unit_converter import units_compatible

DEFAULT_RGCN_CKPT_PATH = get_runtime_data_dir() / "models" / "rgcn_boq_ckpt.pt"

RELATIONS = (
    "IN_CATEGORY",
    "IN_DOMAIN",
    "IN_PROVINCE",
    "MAPS_TO_QUOTA",
    "USES_RESOURCE",
)


@dataclass
class HeteroGraphData:
    """异构工程造价知识图谱张量表示（6 类节点 × 10 条双向关系边）."""

    node2id: dict[str, int]
    item_id_to_node: dict[str, int]
    node_features: torch.Tensor
    schema_node_ids: list[int]
    schema_key_to_id: dict[str, int]
    adjs: list[torch.Tensor]  # length = 2 * len(RELATIONS) = 10
    num_nodes: int
    edge_count: int
    relation_counts: dict[str, int]


def _hashed_feature(key: str, dim: int) -> np.ndarray:
    h = hashlib.sha256(key.encode("utf-8")).digest()
    seed = int.from_bytes(h[:8], "little")
    rng = np.random.default_rng(seed)
    vec = rng.normal(0.0, 1.0, size=dim).astype(np.float32)
    norm = np.linalg.norm(vec)
    return vec / max(norm, 1e-8)


def _build_norm_sparse_adj(edges: list[tuple[int, int]], num_nodes: int) -> torch.Tensor:
    if not edges:
        idx = torch.zeros((2, 0), dtype=torch.long)
        vals = torch.zeros((0,), dtype=torch.float32)
        return torch.sparse_coo_tensor(idx, vals, size=(num_nodes, num_nodes)).coalesce()

    deg = np.zeros(num_nodes, dtype=np.float32)
    for dst, _src in edges:
        deg[dst] += 1.0
    rows = [e[0] for e in edges]
    cols = [e[1] for e in edges]
    vals = [1.0 / max(deg[r], 1.0) for r in rows]
    idx_t = torch.tensor([rows, cols], dtype=torch.long)
    val_t = torch.tensor(vals, dtype=torch.float32)
    return torch.sparse_coo_tensor(idx_t, val_t, size=(num_nodes, num_nodes)).coalesce()


def build_boq_hetero_graph(
    items: list[StandardizedBoqItem],
    projects: list[ProjectContext],
    dense_vectors: dict[str, np.ndarray],
) -> HeteroGraphData:
    """根据训练折中的标准清单、L0 宏观上下文与 L2 工料机定额构成，构建零泄露异构知识图谱."""
    proj_map = {p.project_id: p for p in projects}
    first_vec = next(iter(dense_vectors.values()), None)
    feat_dim = int(len(first_vec)) if first_vec is not None else 64

    node2id: dict[str, int] = {}
    item_id_to_node: dict[str, int] = {}
    schema_key_to_id: dict[str, int] = {}
    schema_node_ids: list[int] = []
    feat_list: list[np.ndarray] = []

    def get_node(key: str, is_item: bool = False, item_id: Optional[str] = None) -> int:
        if key in node2id:
            return node2id[key]
        nid = len(node2id)
        node2id[key] = nid
        if is_item and item_id is not None:
            item_id_to_node[item_id] = nid
            vec = dense_vectors.get(item_id)
            if vec is None or len(vec) != feat_dim:
                vec = _hashed_feature(f"item:{item_id}", feat_dim)
            feat_list.append(np.asarray(vec, dtype=np.float32))
        else:
            schema_node_ids.append(nid)
            schema_key_to_id[key] = nid
            feat_list.append(_hashed_feature(key, feat_dim))
        return nid

    rel_edges: dict[int, list[tuple[int, int]]] = {i: [] for i in range(len(RELATIONS) * 2)}
    rel_counts: dict[str, int] = {r: 0 for r in RELATIONS}

    def add_bidir(src: int, dst: int, rel_idx: int, rel_name: str) -> None:
        # 双向消息传递：dst <- src (forward) 与 src <- dst (reverse)
        rel_edges[rel_idx].append((dst, src))
        rel_edges[rel_idx + len(RELATIONS)].append((src, dst))
        rel_counts[rel_name] += 1

    for it in items:
        i_nid = get_node(f"item:{it.item_id}", is_item=True, item_id=it.item_id)

        cat_nid = get_node(f"cat:{it.category_code}")
        add_bidir(i_nid, cat_nid, 0, "IN_CATEGORY")

        dom_nid = get_node(f"dom:{it.domain}")
        add_bidir(i_nid, dom_nid, 1, "IN_DOMAIN")

        proj = proj_map.get(it.project_id)
        prov = proj.province if proj else "未指定"
        prov_nid = get_node(f"prov:{prov}")
        add_bidir(i_nid, prov_nid, 2, "IN_PROVINCE")

        if it.breakdown:
            for qcode in it.breakdown.quota_codes[:4]:
                q_nid = get_node(f"quota:{qcode}")
                add_bidir(i_nid, q_nid, 3, "MAPS_TO_QUOTA")
            for rline in it.breakdown.resource_lines:
                if rline.resource_type in ("材料", "价差", "人工") and rline.quantity > 0 and rline.name != "合计":
                    res_nid = get_node(f"res:{rline.name}")
                    add_bidir(i_nid, res_nid, 4, "USES_RESOURCE")

    num_nodes = len(node2id)
    node_features = torch.tensor(np.vstack(feat_list), dtype=torch.float32)
    adjs = [_build_norm_sparse_adj(rel_edges[r], num_nodes) for r in range(len(RELATIONS) * 2)]
    edge_count = sum(rel_counts.values()) * 2

    return HeteroGraphData(
        node2id=node2id,
        item_id_to_node=item_id_to_node,
        node_features=node_features,
        schema_node_ids=schema_node_ids,
        schema_key_to_id=schema_key_to_id,
        adjs=adjs,
        num_nodes=num_nodes,
        edge_count=edge_count,
        relation_counts=rel_counts,
    )


class BasisRgcnLayer(nn.Module):
    """Schlichtkrull (2018) Basis-Regularized R-GCN 关系图卷积层."""

    def __init__(self, hidden_dim: int, num_relations: int, num_bases: int = 4) -> None:
        super().__init__()
        self.hidden_dim = hidden_dim
        self.num_relations = num_relations
        self.num_bases = num_bases
        self.self_loop = nn.Linear(hidden_dim, hidden_dim, bias=True)
        self.bases = nn.Parameter(torch.empty(num_bases, hidden_dim, hidden_dim))
        self.rel_comp = nn.Parameter(torch.empty(num_relations, num_bases))
        self.norm = nn.LayerNorm(hidden_dim)
        nn.init.xavier_uniform_(self.bases, gain=0.6)
        nn.init.xavier_uniform_(self.rel_comp, gain=0.6)

    def forward(self, h: torch.Tensor, adjs: list[torch.Tensor]) -> torch.Tensor:
        out = self.self_loop(h)
        weights = torch.einsum("rb,bij->rij", self.rel_comp, self.bases)
        for r_idx, adj in enumerate(adjs):
            if adj._nnz() == 0:
                continue
            msg = torch.sparse.mm(adj, h @ weights[r_idx])
            out = out + 0.5 * msg
        return self.norm(F.relu(out) + h)


class HeteroRgcnModel(nn.Module):
    """2 层可训练 R-GCN 异构图编码器 + 可微硬规格衰减温度 alpha + 四路评分融合门控."""

    def __init__(self, input_dim: int, num_schema_nodes: int, hidden_dim: int = 64) -> None:
        super().__init__()
        self.in_proj = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.LayerNorm(hidden_dim),
            nn.GELU(),
        )
        self.schema_emb = nn.Embedding(max(1, num_schema_nodes), hidden_dim)
        nn.init.normal_(self.schema_emb.weight, std=0.08)

        self.layer1 = BasisRgcnLayer(hidden_dim, num_relations=len(RELATIONS) * 2, num_bases=4)
        self.layer2 = BasisRgcnLayer(hidden_dim, num_relations=len(RELATIONS) * 2, num_bases=4)
        self.out_proj = nn.Linear(hidden_dim, hidden_dim)

        # 可微硬规格衰减核陡峭度 alpha = softplus(raw_alpha) + 1.0
        self.raw_alpha = nn.Parameter(torch.tensor(0.5))
        # 可学习评分融合门控: [rgcn_graph, feat_kernel, sem_dense, ctx]
        self.fusion_logits = nn.Parameter(torch.tensor([0.2, 1.1, 0.2, -0.3], dtype=torch.float32))
        self.pair_temp = nn.Parameter(torch.tensor(8.0, dtype=torch.float32))

    @property
    def learned_alpha(self) -> float:
        return float((F.softplus(self.raw_alpha) + 1.0).item())

    def fusion_weights(self) -> list[float]:
        w = F.softmax(self.fusion_logits, dim=0)
        return [round(float(x), 4) for x in w.detach().cpu().tolist()]

    def encode_graph(self, g: HeteroGraphData) -> torch.Tensor:
        h = self.in_proj(g.node_features)
        if g.schema_node_ids:
            idx_t = torch.tensor(g.schema_node_ids, dtype=torch.long, device=h.device)
            schema_ids = torch.arange(len(g.schema_node_ids), dtype=torch.long, device=h.device)
            h = h.clone()
            h[idx_t] = 0.5 * h[idx_t] + 0.5 * self.schema_emb(schema_ids)
        h1 = self.layer1(h, g.adjs)
        h2 = self.layer2(h1, g.adjs)
        return F.normalize(self.out_proj(h2), p=2, dim=-1)


@dataclass
class RgcnTrainingPair:
    q_idx: int
    pos_idx: int
    neg_idx: int
    pos_rel_diffs: list[float]
    pos_has_cat_mismatch: bool
    pos_name_sim: float
    pos_sem_sim: float
    neg_rel_diffs: list[float]
    neg_has_cat_mismatch: bool
    neg_name_sim: float
    neg_sem_sim: float


def _extract_pair_features(
    a: StandardizedBoqItem,
    b: StandardizedBoqItem,
    dense_vecs: dict[str, np.ndarray],
) -> tuple[list[float], bool, float, float]:
    rel_diffs: list[float] = []
    text_conflict = False
    all_keys = set(a.slots.keys()) | set(b.slots.keys())
    for k in all_keys:
        sa = a.slots.get(k)
        sb = b.slots.get(k)
        if sa and sb:
            if sa.numeric_value is not None and sb.numeric_value is not None:
                denom = max(abs(sa.numeric_value), abs(sb.numeric_value), 1e-6)
                rel_diffs.append(abs(sa.numeric_value - sb.numeric_value) / denom)
            elif sa.hard_constraint:
                va = str(sa.normalized_value).strip().upper()
                vb = str(sb.normalized_value).strip().upper()
                if va != vb and va not in vb and vb not in va:
                    text_conflict = True
    name_sim = 1.0 if a.standard_name == b.standard_name else (
        0.82 if (a.standard_name in b.standard_name or b.standard_name in a.standard_name) else 0.25
    )
    va = dense_vecs.get(a.item_id)
    vb = dense_vecs.get(b.item_id)
    sem_sim = float(max(0.0, np.dot(va, vb))) if (va is not None and vb is not None) else 0.5
    return rel_diffs, text_conflict, name_sim, sem_sim


def _build_contrastive_pairs(
    items: list[StandardizedBoqItem],
    dense_vecs: dict[str, np.ndarray],
    item_id_to_node: dict[str, int],
    max_triplets: int = 1200,
    seed: int = 42,
) -> list[RgcnTrainingPair]:
    rng = random.Random(seed)
    by_cat: dict[str, list[StandardizedBoqItem]] = {}
    for it in items:
        by_cat.setdefault(it.category_code, []).append(it)

    triplets: list[RgcnTrainingPair] = []
    for cat_code, group in by_cat.items():
        if len(group) < 2:
            continue
        for i, q in enumerate(group):
            q_price = q.normalized_unit_price_yuan or 0.0
            pos_cands: list[StandardizedBoqItem] = []
            hard_negs: list[StandardizedBoqItem] = []
            for j, cand in enumerate(group):
                if i == j:
                    continue
                if not units_compatible(q.standard_unit, cand.standard_unit):
                    hard_negs.append(cand)
                    continue
                shared = set(q.slots.keys()) & set(cand.slots.keys())
                conflict = any(slot_similarity(q.slots[k], cand.slots[k])[1] for k in shared)
                c_price = cand.normalized_unit_price_yuan or 0.0
                price_diff = abs(q_price - c_price) / max(q_price, c_price, 1.0)
                if not conflict and (q.standard_name == cand.standard_name or price_diff <= 0.22):
                    pos_cands.append(cand)
                elif conflict or price_diff > 0.35:
                    hard_negs.append(cand)

            if not pos_cands:
                # 自监督同类增强：允许与同类最接近样本组成正对
                pos_cands = [c for c in group if c.item_id != q.item_id][:2]
            if not hard_negs:
                other_pool = [it for it in items if it.category_code != cat_code]
                if other_pool:
                    hard_negs = rng.sample(other_pool, min(3, len(other_pool)))

            if not pos_cands or not hard_negs:
                continue

            for pos in pos_cands[:3]:
                neg = rng.choice(hard_negs)
                p_rd, p_tc, p_ns, p_ss = _extract_pair_features(q, pos, dense_vecs)
                n_rd, n_tc, n_ns, n_ss = _extract_pair_features(q, neg, dense_vecs)
                triplets.append(
                    RgcnTrainingPair(
                        q_idx=item_id_to_node[q.item_id],
                        pos_idx=item_id_to_node[pos.item_id],
                        neg_idx=item_id_to_node[neg.item_id],
                        pos_rel_diffs=p_rd,
                        pos_has_cat_mismatch=p_tc,
                        pos_name_sim=p_ns,
                        pos_sem_sim=p_ss,
                        neg_rel_diffs=n_rd,
                        neg_has_cat_mismatch=n_tc,
                        neg_name_sim=n_ns,
                        neg_sem_sim=n_ss,
                    )
                )
    if len(triplets) > max_triplets:
        triplets = rng.sample(triplets, max_triplets)
    return triplets


class TrainedRgcnPredictor:
    """加载或持有已训练 2 层 R-GCN 检查点，执行零泄露诱导式图嵌入推理."""

    def __init__(
        self,
        model_state: dict[str, Any],
        node_embs: np.ndarray,
        item_id_to_node: dict[str, int],
        schema_key_to_id: dict[str, int],
        learned_alpha: float,
        fusion_weights: list[float],
        input_dim: int,
        hidden_dim: int,
        train_metrics: Optional[dict[str, Any]] = None,
    ) -> None:
        self.node_embs = np.asarray(node_embs, dtype=np.float32)
        self.item_id_to_node = dict(item_id_to_node)
        self.schema_key_to_id = dict(schema_key_to_id)
        self.learned_alpha = float(learned_alpha)
        self.fusion_weights = list(fusion_weights)
        self.input_dim = int(input_dim)
        self.hidden_dim = int(hidden_dim)
        self.train_metrics = train_metrics or {}

        self._model = HeteroRgcnModel(
            input_dim=self.input_dim,
            num_schema_nodes=max(1, len(self.schema_key_to_id)),
            hidden_dim=self.hidden_dim,
        )
        self._model.load_state_dict(model_state)
        self._model.eval()
        self._schema_embs = self.node_embs

    def encode_inductive_query(
        self,
        query: StandardizedBoqItem,
        query_dense_vec: Optional[np.ndarray],
        province: str = "未指定",
    ) -> np.ndarray:
        """对图外未见新清单进行 1-Hop 诱导式 R-GCN 邻域聚合并投影至同一图嵌入空间."""
        vec = query_dense_vec
        if vec is None or len(vec) != self.input_dim:
            vec = _hashed_feature(f"query:{query.standard_name}:{query.canonical_search_text}", self.input_dim)
        with torch.no_grad():
            x = torch.tensor(vec, dtype=torch.float32).unsqueeze(0)
            h0 = self._model.in_proj(x)
            # 聚合图内已学得的类别 / 领域 / 省份实体节点嵌入作为诱导上下文消息
            nbr_keys = [
                (0, f"cat:{query.category_code}"),
                (1, f"dom:{query.domain}"),
                (2, f"prov:{province}"),
            ]
            agg = self._model.layer1.self_loop(h0)
            weights1 = torch.einsum("rb,bij->rij", self._model.layer1.rel_comp, self._model.layer1.bases)
            cnt = 0
            for rel_idx, skey in nbr_keys:
                nid = self.schema_key_to_id.get(skey)
                if nid is not None and nid < len(self._schema_embs):
                    nbr_vec = torch.tensor(self._schema_embs[nid], dtype=torch.float32).unsqueeze(0)
                    agg = agg + 0.35 * (nbr_vec @ weights1[rel_idx])
                    cnt += 1
            if cnt == 0:
                agg = h0
            h1 = self._model.layer1.norm(F.relu(agg) + h0)
            z = F.normalize(self._model.out_proj(h1), p=2, dim=-1)
            return z.squeeze(0).cpu().numpy().astype(np.float32)

    def graph_similarity(self, query_rgcn_vec: np.ndarray, candidate_item_id: str) -> float:
        nid = self.item_id_to_node.get(candidate_item_id)
        if nid is None or nid >= len(self.node_embs):
            return 0.5
        cos = float(np.dot(query_rgcn_vec, self.node_embs[nid]))
        return round(min(1.0, max(0.0, (cos + 1.0) * 0.5)), 4)

    def save(self, ckpt_path: Path = DEFAULT_RGCN_CKPT_PATH) -> Path:
        ckpt_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "model_state": self._model.state_dict(),
            "node_embs": self.node_embs,
            "item_id_to_node": self.item_id_to_node,
            "schema_key_to_id": self.schema_key_to_id,
            "learned_alpha": self.learned_alpha,
            "fusion_weights": self.fusion_weights,
            "input_dim": self.input_dim,
            "hidden_dim": self.hidden_dim,
            "train_metrics": self.train_metrics,
        }
        torch.save(payload, str(ckpt_path))
        return ckpt_path

    @classmethod
    def load(cls, ckpt_path: Path = DEFAULT_RGCN_CKPT_PATH) -> Optional["TrainedRgcnPredictor"]:
        if not ckpt_path.exists():
            return None
        try:
            payload = torch.load(str(ckpt_path), map_location="cpu", weights_only=False)
            return cls(**payload)
        except Exception:
            return None


def train_rgcn_model(
    items: list[StandardizedBoqItem],
    projects: list[ProjectContext],
    dense_vectors: dict[str, np.ndarray],
    *,
    epochs: int = 45,
    hidden_dim: int = 64,
    lr: float = 0.015,
    save_path: Optional[Path] = None,
    seed: int = 42,
) -> TrainedRgcnPredictor:
    """在训练集（零测试泄露）上端到端训练 2 层异构图 R-GCN + 对比学习可微硬规格核参数."""
    t0 = time.perf_counter()
    torch.manual_seed(seed)
    np.random.seed(seed)

    g = build_boq_hetero_graph(items, projects, dense_vectors)
    input_dim = int(g.node_features.shape[1])
    triplets = _build_contrastive_pairs(items, dense_vectors, g.item_id_to_node, seed=seed)

    model = HeteroRgcnModel(
        input_dim=input_dim,
        num_schema_nodes=max(1, len(g.schema_node_ids)),
        hidden_dim=hidden_dim,
    )
    opt = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=1e-4)

    loss_history: list[dict[str, float]] = []
    if triplets:
        q_idx_t = torch.tensor([t.q_idx for t in triplets], dtype=torch.long)
        pos_idx_t = torch.tensor([t.pos_idx for t in triplets], dtype=torch.long)
        neg_idx_t = torch.tensor([t.neg_idx for t in triplets], dtype=torch.long)

        def _kernel_tensor(rd_list: list[float], tc: bool, alpha_t: torch.Tensor) -> torch.Tensor:
            if tc:
                return torch.tensor(0.05, dtype=torch.float32)
            if not rd_list:
                return torch.tensor(0.95, dtype=torch.float32)
            max_rd = max(rd_list)
            return torch.exp(-alpha_t * max_rd)

        pos_ns_t = torch.tensor([t.pos_name_sim for t in triplets], dtype=torch.float32)
        neg_ns_t = torch.tensor([t.neg_name_sim for t in triplets], dtype=torch.float32)
        pos_ss_t = torch.tensor([t.pos_sem_sim for t in triplets], dtype=torch.float32)
        neg_ss_t = torch.tensor([t.neg_sem_sim for t in triplets], dtype=torch.float32)

        model.train()
        for ep in range(1, epochs + 1):
            opt.zero_grad()
            emb = model.encode_graph(g)
            z_q = emb[q_idx_t]
            z_p = emb[pos_idx_t]
            z_n = emb[neg_idx_t]

            sim_p = (z_q * z_p).sum(dim=-1)
            sim_n = (z_q * z_n).sum(dim=-1)

            alpha_t = F.softplus(model.raw_alpha) + 1.0
            k_p = torch.stack([_kernel_tensor(t.pos_rel_diffs, t.pos_has_cat_mismatch, alpha_t) for t in triplets])
            k_n = torch.stack([_kernel_tensor(t.neg_rel_diffs, t.neg_has_cat_mismatch, alpha_t) for t in triplets])

            w = F.softmax(model.fusion_logits, dim=0)
            score_p = w[0] * ((sim_p + 1.0) * 0.5) + w[1] * (0.75 * k_p + 0.25 * pos_ns_t) + w[2] * pos_ss_t + w[3] * 0.85
            score_n = w[0] * ((sim_n + 1.0) * 0.5) + w[1] * (0.75 * k_n + 0.25 * neg_ns_t) + w[2] * neg_ss_t + w[3] * 0.70

            # InfoNCE 对比图损失 + 边际排序损失（驱动正对接近、硬冲突负对远离）
            temp = torch.clamp(model.pair_temp, 2.0, 20.0)
            logits_infonce = torch.stack([sim_p * temp, sim_n * temp], dim=-1)
            labels_infonce = torch.zeros(len(triplets), dtype=torch.long)
            loss_graph = F.cross_entropy(logits_infonce, labels_infonce)
            loss_margin = F.relu(0.32 - (score_p - score_n)).mean()
            loss = loss_graph + 1.5 * loss_margin

            loss.backward()
            opt.step()

            if ep == 1 or ep % 5 == 0 or ep == epochs:
                loss_history.append(
                    {
                        "epoch": ep,
                        "loss": round(float(loss.item()), 4),
                        "graph_loss": round(float(loss_graph.item()), 4),
                        "margin_loss": round(float(loss_margin.item()), 4),
                        "alpha": round(float(alpha_t.item()), 3),
                    }
                )

    model.eval()
    with torch.no_grad():
        final_emb = model.encode_graph(g).cpu().numpy().astype(np.float32)

    elapsed_s = round(time.perf_counter() - t0, 3)
    train_metrics = {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "epochs": epochs,
        "train_items_count": len(items),
        "graph_nodes": g.num_nodes,
        "graph_edges": g.edge_count,
        "relation_counts": g.relation_counts,
        "contrastive_triplets": len(triplets),
        "learned_alpha": round(model.learned_alpha, 3),
        "fusion_weights": model.fusion_weights(),
        "elapsed_sec": elapsed_s,
        "loss_history": loss_history,
    }

    predictor = TrainedRgcnPredictor(
        model_state=model.state_dict(),
        node_embs=final_emb,
        item_id_to_node=g.item_id_to_node,
        schema_key_to_id=g.schema_key_to_id,
        learned_alpha=model.learned_alpha,
        fusion_weights=model.fusion_weights(),
        input_dim=input_dim,
        hidden_dim=hidden_dim,
        train_metrics=train_metrics,
    )
    if save_path is not None:
        predictor.save(save_path)
    return predictor
