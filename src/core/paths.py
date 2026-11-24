import json
import os
from pathlib import Path
import shutil
import sys


def is_frozen() -> bool:
    """判断当前是否运行于 PyInstaller 等打包后的独立可执行环境中."""
    return bool(getattr(sys, "frozen", False))


def get_bundle_root() -> Path:
    """返回只读代码与资源根目录（源码态为仓库根目录，打包态为 sys._MEIPASS）."""
    if is_frozen() and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return Path(__file__).resolve().parents[2]


def get_runtime_home() -> Path:
    """返回打包态可写的用户运行时目录 (~/.boq_intelligence 或环境变量 BOQ_RUNTIME_DIR)."""
    override = os.environ.get("BOQ_RUNTIME_DIR", "").strip()
    if override:
        home = Path(override).expanduser().resolve()
    else:
        home = Path.home() / ".boq_intelligence"
    home.mkdir(parents=True, exist_ok=True)
    return home


def get_runtime_data_dir() -> Path:
    """返回可读写的数据目录；在打包态首次启动时自动释放内置数据库、样本文件及大模型配置."""
    if not is_frozen():
        return get_bundle_root() / "data"

    runtime_data = get_runtime_home() / "data"
    runtime_data.mkdir(parents=True, exist_ok=True)
    bundled_data = get_bundle_root() / "data"
    if not bundled_data.exists():
        return runtime_data

    # 1. 释放预构建的 SQLite 知识库
    bundled_db = bundled_data / "boq_knowledge.db"
    target_db = runtime_data / "boq_knowledge.db"
    if bundled_db.exists() and not target_db.exists():
        shutil.copy2(bundled_db, target_db)

    # 2. 释放内置第三方大模型中转站配置与 Key（若本地尚无 Key 则自动同步内置配置）
    bundled_llm = bundled_data / "llm_settings.json"
    target_llm = runtime_data / "llm_settings.json"
    if bundled_llm.exists():
        need_seed = not target_llm.exists()
        if not need_seed:
            try:
                cur_cfg = json.loads(target_llm.read_text(encoding="utf-8"))
                bud_cfg = json.loads(bundled_llm.read_text(encoding="utf-8"))
                if not cur_cfg.get("api_key") and bud_cfg.get("api_key"):
                    need_seed = True
            except Exception:
                need_seed = True
        if need_seed:
            shutil.copy2(bundled_llm, target_llm)

    # 3. 释放配套原始工程样表（供重新入库或查看使用）
    for item in bundled_data.iterdir():
        if item.is_file() and item.name not in ("boq_knowledge.db", "llm_settings.json", ".DS_Store"):
            dst = runtime_data / item.name
            if not dst.exists():
                shutil.copy2(item, dst)

    return runtime_data
