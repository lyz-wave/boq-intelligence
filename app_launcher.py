import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

import uvicorn

from src.core.paths import get_runtime_home, is_frozen


def _redirect_windowed_stdio() -> None:
    """在 PyInstaller --windowed 无控制台模式下，将 stdout/stderr 重定向至日志文件避免 isatty 异常."""
    if not is_frozen():
        return
    log_path = get_runtime_home() / "app.log"
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    if sys.stdout is None or not hasattr(sys.stdout, "write"):
        sys.stdout = log_file
    if sys.stderr is None or not hasattr(sys.stderr, "write"):
        sys.stderr = log_file


def _is_port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _open_browser_when_ready(url: str) -> None:
    if os.environ.get("BOQ_NO_BROWSER") == "1":
        return
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"{url}/api/overview", timeout=1.0) as resp:
                if resp.status == 200:
                    webbrowser.open(url)
                    return
        except Exception:
            time.sleep(0.25)


def main() -> None:
    _redirect_windowed_stdio()
    host = os.environ.get("BOQ_HOST", "127.0.0.1")
    port = int(os.environ.get("BOQ_PORT", "8000"))
    url = f"http://{host}:{port}"

    if _is_port_open(host, port):
        if os.environ.get("BOQ_NO_BROWSER") != "1":
            webbrowser.open(url)
        return

    threading.Thread(target=_open_browser_when_ready, args=(url,), daemon=True).start()

    # 显式导入 FastAPI 应用对象，确保 PyInstaller 打包入口静态可追踪
    from src.api.main import app

    uvicorn.run(app, host=host, port=port, log_config=None)


if __name__ == "__main__":
    main()
