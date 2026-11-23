import re


def tokenize_boq_text(text: str) -> list[str]:
    """面向工程造价术语与复合型号的混合分词器（保留字母数字规格串 + 中文词元/双字切分）."""
    if not text:
        return []
    clean = text.upper().replace("＊", "×").replace("*", "×")
    tokens: list[str] = []

    # 1. 提取完整型号与数值规格串，如 ZR-YJV, 0.6/1KV, 3×150, 5KM, C25, DN110, 10224
    for m in re.findall(r"[A-Z0-9]+(?:[./×+\-][A-Z0-9]+)*", clean):
        tokens.append(m)
        if len(m) >= 3:
            for sub in re.split(r"[/×+\-]", m):
                if sub and sub != m:
                    tokens.append(sub)

    # 2. 提取中文片段并切分2-3字滑窗以应对同义词（如 清表 / 地表清理 / 土石方运输）
    for seg in re.findall(r"[\u4e00-\u9fff]+", clean):
        tokens.append(seg)
        if len(seg) >= 2:
            for i in range(len(seg) - 1):
                tokens.append(seg[i : i + 2])
            for i in range(len(seg) - 2):
                tokens.append(seg[i : i + 3])
        else:
            tokens.append(seg)
    return tokens
