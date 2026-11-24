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
        body_text = re.sub(
            r"\s+",
            " ",
            " ".join(p for p in [raw_item.raw_name, raw_item.raw_features, raw_item.raw_work_content] if p),
        ).strip()
        body_text = self._fix_cn_linebreaks(body_text).lower()
        sec_text = " ".join(raw_item.section_path).lower()

        best_cat: Optional[dict[str, Any]] = None
        best_score = 0.0
        for cat in ontology.get("categories", []):
            kws = cat.get("trigger_keywords", [])
            body_score = sum(2.0 * len(kw) for kw in kws if kw.lower() in body_text)
            sec_score = sum(0.4 for kw in kws if kw.lower() in sec_text)
            total_score = body_score + sec_score
            if total_score > best_score:
                best_score = total_score
                best_cat = cat
        return best_cat

    @classmethod
    def _fix_cn_linebreaks(cls, text: str) -> str:
        # 合并 PDF/Excel 表格换行导致的中文断字空格
        for broken, fixed in (
            (r"户\s+内", "户内"),
            (r"户\s+外", "户外"),
            (r"室\s+内", "室内"),
            (r"室\s+外", "室外"),
            (r"终\s+端", "终端"),
            (r"电\s+缆", "电缆"),
            (r"混\s+凝\s+土", "混凝土"),
        ):
            text = re.sub(broken, fixed, text)
        return text

    @classmethod
    def _prepare_text(cls, raw_item: RawBoqItem) -> str:
        parts = [" ".join(raw_item.section_path), raw_item.raw_name, raw_item.raw_features, raw_item.raw_work_content]
        joined = " ".join(p for p in parts if p)
        joined = re.sub(r"\s+", " ", joined).strip()
        return cls._fix_cn_linebreaks(joined)

    @classmethod
    def _normalize_value(cls, raw: str, compute_mode: Optional[str]) -> tuple[Any, Optional[float]]:
        clean = raw.replace("＊", "×").replace("*", "×").strip()
        if compute_mode == "cable_area_sum":
            return clean, cls._eval_cable_area(clean)
        if compute_mode == "voltage_kv":
            nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", clean)]
            val = nums[-1] if nums else None
            return clean.upper(), val
        if compute_mode == "length_to_cm":
            num = cls._extract_first_float(clean)
            if num is None:
                return clean, None
            if "mm" in clean.lower() or "毫米" in clean:
                cm_val = round(num / 10.0, 3)
                return cm_val, cm_val
            return num, num
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
