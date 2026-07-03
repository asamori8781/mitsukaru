"""エントリポイント。FastAPIをバックグラウンドスレッドで起動し、pywebviewウィンドウを開く。"""
from __future__ import annotations

import sys
import threading
import time
import urllib.request

import uvicorn
import webview

import config
import db

PORT = 18765

_uvicorn_server: "uvicorn.Server | None" = None


class Api:
    """フロントエンドのJSからpywebviewのネイティブダイアログを呼び出すための橋渡し。"""

    def select_folder(self) -> "str | None":
        window = webview.windows[0]
        result = window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return None
        return result[0]


def _run_server() -> None:
    import server  # 起動時のDBスキーマ初期化・バージョンチェックはserver側のstartupイベントで実施

    global _uvicorn_server
    uv_config = uvicorn.Config(server.app, host="127.0.0.1", port=PORT, log_level="warning")
    _uvicorn_server = uvicorn.Server(uv_config)
    _uvicorn_server.run()


def _wait_for_server_ready(timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/status", timeout=1)
            return True
        except Exception:
            time.sleep(0.2)
    return False


def _show_error_window(message: str) -> None:
    html = f"""
    <html><head><meta charset="utf-8"><title>ミツカル - 起動エラー</title></head>
    <body style="font-family: sans-serif; padding: 2em;">
    <h2>起動できませんでした</h2>
    <p>{message}</p>
    </body></html>
    """
    webview.create_window("ミツカル - 起動エラー", html=html, width=600, height=300)
    webview.start()


def main() -> None:
    try:
        db.check_sqlite_version()
    except db.UnsupportedSQLiteError as e:
        _show_error_window(str(e))
        sys.exit(1)

    config.ensure_dirs()

    server_thread = threading.Thread(target=_run_server, daemon=True)
    server_thread.start()

    if not _wait_for_server_ready():
        _show_error_window("サーバーの起動に失敗しました。ログを確認してください。")
        sys.exit(1)

    api = Api()
    webview.create_window(
        "ミツカル", f"http://127.0.0.1:{PORT}/",
        width=1280, height=840, js_api=api,
    )
    webview.start()

    if _uvicorn_server is not None:
        _uvicorn_server.should_exit = True


if __name__ == "__main__":
    main()
