import re
from typing import Any, Optional

from src.core.contracts.boq_item import FeatureDimension, FeatureSlotValue, RawBoqItem
from src.core.registry import PluginRegistry


@PluginRegistry.register("extractors")
class YamlRegexFeatureExtractor:
    """Tier-1 确定性正则与领域本体槽位抽取插件（毫秒级零外部依赖）"""

    extractor_id: str = "yaml_regex_extractor"
    priority: int = 10

    def extract(self, raw_item: RawBoqItem, ontology: dict[str, Any]) -> dict[str, FeatureSlotValue]:
        text = self._prepare_text(raw_item)
        category = self.match_category(raw_item, ontology)
        if not category:
            return {}

        slots: dict[str, FeatureSlotValue] = {}
        for slot_cfg in category.get("slots", []):
            pattern = slot_cfg.get("regex")
            if not pattern:
                continue
            m = re.search(pattern, text, flags=re.IGNORECASE)
            if not m:
                continue
            captured = next((g for g in m.groups() if g is not None), m.group(0))
            captured = captured.strip()
            compute_mode = slot_cfg.get("compute_mode")
            norm_val, num_val = self._normalize_value(captured, compute_mode)

            slots[slot_cfg["key"]] = FeatureSlotValue(
                slot_key=slot_cfg["key"],
                slot_label=slot_cfg.get("label", slot_cfg["key"]),
                dimension=FeatureDimension(slot_cfg.get("dimension", "entity_spec")),
                raw_text=m.group(0).strip(),
                normalized_value=norm_val,
                numeric_value=num_val,
                numeric_unit=slot_cfg.get("unit"),
                hard_constraint=bool(slot_cfg.get("hard_constraint", False)),
                ahp_weight=float(slot_cfg.get("weight", 0.0)),
                extractor_source="regex",
            )
        return slots

    def match_category(self, raw_item: RawBoqItem, ontology: dict[str, Any]) -> Optional[dict[str, Any]]:
        text = self._prepare_text(raw_item)
        best_cat: Optional[dict[str, Any]] = None
        best_hits = 0
        for cat in ontology.get("categories", []):
            hits = sum(1 for kw in cat.get("trigger_keywords", []) if kw.lower() in text.lower())
            if hits > best_hits:
                best_hits = hits
                best_cat = cat
        return best_cat

    @staticmethod
    def _prepare_text(raw_item: RawBoqItem) -> str:
        parts = [" ".join(raw_item.section_path), raw_item.raw_name, raw_item.raw_features, raw_item.raw_work_content]
        joined = " ".join(p for p in parts if p)
        return re.sub(r"\s+", " ", joined).strip()

    @classmethod
    def _normalize_value(cls, raw: str, compute_mode: Optional[str]) -> tuple[Any, Optional[float]]:
        clean = raw.replace("＊", "×").replace("*", "×").strip()
        if compute_mode == "cable_area_sum":
            return clean, cls._eval_cable_area(clean)
        if compute_mode == "distance_to_m":
            num = cls._extract_first_float(clean)
            if num is None:
                return clean, None
            if "km" in clean.lower() or "公里" in clean:
                return round(num * 1000.0, 2), round(num * 1000.0, 2)
            return num, num
        if compute_mode == "range_midpoint":
            nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", clean)]
            if len(nums) >= 2:
                mid = round((nums[0] + nums[1]) / 2.0, 3)
                return clean, mid
            if len(nums) == 1:
                return clean, nums[0]
        if compute_mode == "float":
            num = cls._extract_first_float(clean)
            return num if num is not None else clean, num
        return clean.upper() if re.fullmatch(r"[A-Za-z0-9./\-+]+", clean) else clean, None

    @staticmethod
    def _eval_cable_area(expr: str) -> Optional[float]:
        # 支持 3×150+1×95 -> 545.0, 5×2×1.5 -> 15.0, 铜鼻子150 -> 150.0, 35×4mm -> 140.0
        s = expr.replace("×", "*").replace("mm", "").replace(" ", "")
        if "A" in s:
            m = re.search(r"(\d+(?:\.\d+)?)A", s)
            return float(m.group(1)) if m else None
        terms = s.split("+")
        total = 0.0
        parsed_any = False
        for term in terms:
            factors = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", term)]
            if not factors:
                continue
            parsed_any = True
            prod = 1.0
            for f in factors:
                prod *= f
            total += prod
        return round(total, 3) if parsed_any else None

    @staticmethod
    def _extract_first_float(text: str) -> Optional[float]:
        m = re.search(r"\d+(?:\.\d+)?", text)
        return float(m.group(0)) if m else None
