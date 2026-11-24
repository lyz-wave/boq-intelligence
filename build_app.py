from pathlib import Path
import shutil
import subprocess
import sys


ROOT_DIR = Path(__file__).resolve().parent
DIST_DIR = ROOT_DIR / "dist"
BUILD_DIR = ROOT_DIR / "build"
APP_NAME = "BOQ Intelligence"


def build() -> None:
    sep = ";" if sys.platform.startswith("win") else ":"
    add_data = [
        f"{ROOT_DIR / 'configs'}{sep}configs",
        f"{ROOT_DIR / 'data'}{sep}data",
        f"{ROOT_DIR / 'src' / 'api' / 'static'}{sep}src/api/static",
        f"{ROOT_DIR / 'project_showcase.html'}{sep}.",
    ]

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--onedir",
        "--name",
        APP_NAME,
        "--collect-submodules",
        "src",
        "--hidden-import",
        "uvicorn.logging",
        "--hidden-import",
        "uvicorn.loops.auto",
        "--hidden-import",
        "uvicorn.protocols.http.auto",
        "--hidden-import",
        "uvicorn.protocols.websockets.auto",
        "--hidden-import",
        "uvicorn.lifespan.on",
        "--hidden-import",
        "sklearn.utils._typedefs",
        "--hidden-import",
        "sklearn.neighbors._partition_nodes",
        "--hidden-import",
        "scipy.special._cdflib",
        "--hidden-import",
        "multipart",
        "--hidden-import",
        "python_multipart",
        "--hidden-import",
        "xlrd",
        "--hidden-import",
        "openpyxl",
        "--hidden-import",
        "docx",
        "--hidden-import",
        "pypdf",
        "--hidden-import",
        "httpx",
        "--hidden-import",
        "yaml",
    ]
    for item in add_data:
        cmd.extend(["--add-data", item])
    cmd.append(str(ROOT_DIR / "app_launcher.py"))

    print("Running:", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(ROOT_DIR))

    if sys.platform == "darwin":
        app_path = DIST_DIR / f"{APP_NAME}.app"
        # 清理 Finder 扩展属性与 .DS_Store，确保 ad-hoc codesign 成功
        for ds in app_path.rglob(".DS_Store"):
            ds.unlink(missing_ok=True)
        subprocess.run(["xattr", "-cr", str(app_path)], check=True)
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app_path)], check=True)
        zip_path = DIST_DIR / "BOQ-Intelligence-macOS.zip"
        if zip_path.exists():
            zip_path.unlink()
        # 使用 ditto 保留 macOS .app 符号链接与可执行权限
        subprocess.run(
            ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app_path), str(zip_path)],
            check=True,
        )
        print(f"macOS App built & signed: {app_path}")
        print(f"Shareable ZIP:            {zip_path}")
    else:
        exe_dir = DIST_DIR / APP_NAME
        zip_base = DIST_DIR / "BOQ-Intelligence-Windows"
        shutil.make_archive(str(zip_base), "zip", root_dir=str(DIST_DIR), base_dir=APP_NAME)
        print(f"Executable folder: {exe_dir}")


if __name__ == "__main__":
    build()
