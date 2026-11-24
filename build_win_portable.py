#!/usr/bin/env python3
"""
Build a standalone, zero-install Windows x64 portable package for BOQ Intelligence
directly from macOS/Linux using the official Python 3.11.9 Embeddable Runtime and
pre-downloaded win_amd64 binary wheels.

Output:
  dist/BOQ-Intelligence-Windows/
  dist/BOQ-Intelligence-Windows.zip
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
BUILD_CACHE = ROOT_DIR / "build" / "win_cache"
WHEELS_DIR = BUILD_CACHE / "wheels"
DIST_DIR = ROOT_DIR / "dist"
STAGE_DIR = DIST_DIR / "BOQ-Intelligence-Windows"
ZIP_PATH = DIST_DIR / "BOQ-Intelligence-Windows.zip"

PYTHON_EMBED_URL = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip"
PYTHON_EMBED_ZIP = BUILD_CACHE / "python-3.11.9-embed-amd64.zip"

RUNTIME_PACKAGES = [
    "fastapi>=0.115.0",
    "uvicorn>=0.30.0",
    "pydantic>=2.10,<3.0",
    "pyyaml>=6.0",
    "xlrd>=2.0.1",
    "openpyxl>=3.1.2",
    "python-docx>=1.1.0",
    "PyMuPDF>=1.24.0",
    "pypdf>=4.0.0",
    "python-multipart>=0.0.9",
    "numpy>=1.26.0",
    "scikit-learn>=1.5.0",
    "scipy>=1.11.0",
    "httpx>=0.27.0",
]


def download_embeddable_python() -> None:
    """Download the official Windows x64 embeddable Python 3.11.9 zip if not cached."""
    BUILD_CACHE.mkdir(parents=True, exist_ok=True)
    if PYTHON_EMBED_ZIP.exists() and PYTHON_EMBED_ZIP.stat().st_size > 1_000_000:
        print(f"[cache] Reusing {PYTHON_EMBED_ZIP}")
        return
    print(f"[download] Fetching {PYTHON_EMBED_URL} ...")
    curl_bin = shutil.which("curl")
    if curl_bin:
        subprocess.run(
            [curl_bin, "-fsSL", "-o", str(PYTHON_EMBED_ZIP), PYTHON_EMBED_URL],
            check=True,
        )
    else:
        req = urllib.request.Request(
            PYTHON_EMBED_URL,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp, open(PYTHON_EMBED_ZIP, "wb") as fh:
            shutil.copyfileobj(resp, fh)
    print(f"[download] Saved {PYTHON_EMBED_ZIP} ({PYTHON_EMBED_ZIP.stat().st_size:,} bytes)")


def download_windows_wheels() -> None:
    """Download win_amd64 / pure-python wheels for Python 3.11 into cache."""
    WHEELS_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--only-binary=:all:",
        "--platform",
        "win_amd64",
        "--python-version",
        "311",
        "--implementation",
        "cp",
        "--abi",
        "cp311",
        "--dest",
        str(WHEELS_DIR),
        *RUNTIME_PACKAGES,
    ]
    print("[pip] Downloading Windows cp311 win_amd64 wheels...")
    subprocess.run(cmd, check=True)


def prepare_runtime() -> None:
    """Extract embedded Python and all downloaded wheels into runtime/."""
    runtime_dir = STAGE_DIR / "runtime"
    site_packages = runtime_dir / "Lib" / "site-packages"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    site_packages.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(PYTHON_EMBED_ZIP, "r") as zf:
        zf.extractall(runtime_dir)

    pth_path = runtime_dir / "python311._pth"
    pth_path.write_text(
        "python311.zip\r\n.\r\n..\r\nLib/site-packages\r\nimport site\r\n",
        encoding="utf-8",
    )

    wheel_files = sorted(WHEELS_DIR.glob("*.whl"))
    if not wheel_files:
        raise RuntimeError(f"No wheel files found in {WHEELS_DIR}")

    print(f"[extract] Extracting {len(wheel_files)} wheels into runtime/Lib/site-packages/ ...")
    for whl in wheel_files:
        with zipfile.ZipFile(whl, "r") as zf:
            zf.extractall(site_packages)


def _ignore_copy(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n in {"__pycache__", ".DS_Store"} or n.endswith(".pyc")}


def copy_project_payload() -> None:
    """Copy code, configs, built-in data (knowledge DB + LLM settings + raw files), and showcase."""
    for folder_name in ("src", "configs", "data"):
        src_dir = ROOT_DIR / folder_name
        dst_dir = STAGE_DIR / folder_name
        if not src_dir.exists():
            raise FileNotFoundError(f"Required directory missing: {src_dir}")
        shutil.copytree(src_dir, dst_dir, ignore=_ignore_copy)

    for file_name in ("project_showcase.html", "app_launcher.py", "run.py"):
        src_file = ROOT_DIR / file_name
        if not src_file.exists():
            raise FileNotFoundError(f"Required file missing: {src_file}")
        shutil.copy2(src_file, STAGE_DIR / file_name)


def write_windows_launchers() -> None:
    """Create double-click .bat, silent .vbs, presentation launcher, and README."""
    bat_start = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        'cd /d "%~dp0"\r\n'
        "title BOQ Intelligence - Built-in Knowledge Base & AI Workbench\r\n"
        "echo ============================================================\r\n"
        "echo   BOQ Intelligence (Windows x64 Portable Edition)\r\n"
        "echo   Starting local engine and opening workbench in browser...\r\n"
        "echo   Keep this window open while using the app.\r\n"
        "echo ============================================================\r\n"
        "echo.\r\n"
        'if not exist "runtime\\python.exe" (\r\n'
        "  echo [ERROR] runtime\\python.exe not found.\r\n"
        "  pause\r\n"
        "  exit /b 1\r\n"
        ")\r\n"
        '"runtime\\python.exe" app_launcher.py %*\r\n'
        "if errorlevel 1 (\r\n"
        "  echo.\r\n"
        "  echo [ERROR] Application exited unexpectedly.\r\n"
        "  pause\r\n"
        ")\r\n"
    )
    (STAGE_DIR / "双击启动 BOQ Intelligence.bat").write_text(bat_start, encoding="utf-8")

    vbs_silent = (
        'Set objShell = CreateObject("WScript.Shell")\r\n'
        'Set fso = CreateObject("Scripting.FileSystemObject")\r\n'
        'baseDir = fso.GetParentFolderName(WScript.ScriptFullName)\r\n'
        'objShell.CurrentDirectory = baseDir\r\n'
        'pythonExe = baseDir & "\\runtime\\pythonw.exe"\r\n'
        'appEntry = baseDir & "\\app_launcher.py"\r\n'
        'objShell.Run """" & pythonExe & """ """ & appEntry & """", 0, False\r\n'
    )
    (STAGE_DIR / "无黑框静默启动工作台.vbs").write_text(vbs_silent, encoding="utf-8")

    bat_ppt = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        'cd /d "%~dp0"\r\n'
        'start "" "%~dp0project_showcase.html"\r\n'
    )
    (STAGE_DIR / "双击打开组会汇报PPT.bat").write_text(bat_ppt, encoding="utf-8")

    readme = (
        "BOQ Intelligence - Windows x64 免安装绿色版使用说明\r\n"
        "==================================================\r\n\r\n"
        "1. 本压缩包已内置全部运行环境与数据，无需安装 Python，也无需 pip install：\r\n"
        "   - 内置 Python 3.11.9 x64 独立运行时与全套依赖库 (runtime/)\r\n"
        "   - 内置历史工程量清单知识库 data/boq_knowledge.db 与原始工程参考文件\r\n"
        "   - 内置已配置好的第三方大模型中转站参数 data/llm_settings.json\r\n\r\n"
        "2. 启动方式（解压整个文件夹后再双击运行，切勿在压缩包预览窗口内直接双击）：\r\n"
        "   - 推荐方式 A：双击「双击启动 BOQ Intelligence.bat」（带终端日志，关闭黑框即退出服务）\r\n"
        "   - 桌面体验 B：双击「无黑框静默启动工作台.vbs」（无命令行黑框，自动打开浏览器工作台）\r\n"
        "   - 演示汇报 C：双击「双击打开组会汇报PPT.bat」（直接打开 7 页视觉化汇报 PPT）\r\n\r\n"
        "3. 常见说明：\r\n"
        "   - 默认监听本机 http://127.0.0.1:8000 （若 8000 端口被占用会自动顺延空闲端口）。\r\n"
        "   - 若 Windows Defender / SmartScreen 提示未知来源脚本，点击“更多信息 -> 仍要运行”即可。\r\n"
    )
    (STAGE_DIR / "使用说明-Windows免安装版.txt").write_text(readme, encoding="utf-8")


def create_zip_archive() -> None:
    """Package dist/BOQ-Intelligence-Windows/ into dist/BOQ-Intelligence-Windows.zip."""
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    print(f"[zip] Creating {ZIP_PATH} ...")
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for file_path in sorted(STAGE_DIR.rglob("*")):
            if file_path.is_dir():
                continue
            if file_path.name == ".DS_Store":
                continue
            arcname = Path("BOQ-Intelligence-Windows") / file_path.relative_to(STAGE_DIR)
            zf.write(file_path, arcname.as_posix())
    print(f"[done] Built {ZIP_PATH} ({ZIP_PATH.stat().st_size / (1024 * 1024):.1f} MB)")


def main() -> None:
    if STAGE_DIR.exists():
        shutil.rmtree(STAGE_DIR)
    STAGE_DIR.mkdir(parents=True, exist_ok=True)

    download_embeddable_python()
    download_windows_wheels()
    prepare_runtime()
    copy_project_payload()
    write_windows_launchers()
    create_zip_archive()


if __name__ == "__main__":
    main()
