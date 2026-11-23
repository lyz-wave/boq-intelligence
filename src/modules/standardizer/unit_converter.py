import re
from typing import Any, Optional

# 物理量纲族与基准换算率（用于跨单位相似匹配及自动调价换算）
UNIT_FAMILIES: dict[str, tuple[str, float]] = {
    "m²": ("area", 1.0),
    "hm²": ("area", 10000.0),
    "亩": ("area", 666.666667),
    "m": ("length", 1.0),
    "km": ("length", 1000.0),
    "m³": ("volume", 1.0),
    "100m³": ("volume", 100.0),
    "t": ("mass", 1.0),
    "kg": ("mass", 0.001),
    "株": ("count", 1.0),
    "棵": ("count", 1.0),
    "100棵": ("count", 100.0),
    "个": ("count", 1.0),
    "套": ("count", 1.0),
    "座": ("count", 1.0),
    "台": ("count", 1.0),
    "千块": ("count", 1000.0),
    "项": ("lump_sum", 1.0),
}

GLOBAL_UNIT_ALIASES: dict[str, tuple[str, float]] = {
    "㎡": ("m²", 1.0),
    "m2": ("m²", 1.0),
    "平方米": ("m²", 1.0),
    "公顷": ("hm²", 1.0),
    "hm2": ("hm²", 1.0),
    "hm²": ("hm²", 1.0),
    "立方米": ("m³", 1.0),
    "m3": ("m³", 1.0),
    "方": ("m³", 1.0),
    "米": ("m", 1.0),
    "延长米": ("m", 1.0),
    "吨": ("t", 1.0),
    "千克": ("kg", 1.0),
}


def normalize_engineering_text(text: str) -> str:
    """统一全半角括号与工程乘号，剔除冗余空白."""
    if not text:
        return ""
    s = text.replace("＊", "×").replace("*", "×").replace("（", "(").replace("）", ")")
    s = s.replace("：", ":").replace("；", ";")
    return re.sub(r"\s+", " ", s).strip()


def normalize_unit(
    raw_unit: Optional[str], ontology: Optional[dict[str, Any]] = None
) -> tuple[Optional[str], float]:
    if not raw_unit:
        return None, 1.0
    u = raw_unit.strip()
    if ontology:
        norm_map = ontology.get("unit_normalizations", {})
        if u in norm_map:
            entry = norm_map[u]
            return entry["standard"], float(entry.get("factor", 1.0))
    if u in GLOBAL_UNIT_ALIASES:
        return GLOBAL_UNIT_ALIASES[u]
    return u, 1.0


def units_compatible(u1: Optional[str], u2: Optional[str]) -> bool:
    """判断两个计量单位是否属于同一物理量纲族（可精确换算）."""
    if not u1 or not u2:
        return True
    if u1 == u2:
        return True
    f1 = UNIT_FAMILIES.get(u1)
    f2 = UNIT_FAMILIES.get(u2)
    if f1 and f2:
        return f1[0] == f2[0]
    return False


def unit_conversion_ratio(from_unit: Optional[str], to_unit: Optional[str]) -> float:
    """计算从 from_unit 换算到 to_unit 时，数量应乘的系数（单价则除以该系数）."""
    if not from_unit or not to_unit or from_unit == to_unit:
        return 1.0
    f1 = UNIT_FAMILIES.get(from_unit)
    f2 = UNIT_FAMILIES.get(to_unit)
    if f1 and f2 and f1[0] == f2[0]:
        return f1[1] / f2[1]
    return 1.0
