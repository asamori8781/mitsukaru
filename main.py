"""エントリポイント。FastAPIをバックグラウンドスレッドで起動し、pywebviewウィンドウを開く。"""
from __future__ import annotations

import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime

import uvicorn
import webview

import config
import db

PORT = 18765
STARTUP_ERROR_LOG = config.LOGS_DIR / "startup_error.log"

_uvicorn_server: "uvicorn.Server | None" = None
_startup_error: "str | None" = None


class Api:
    """フロントエンドのJSからpywebviewのネイティブダイアログを呼び出すための橋渡し。"""

    def select_folder(self) -> "str | None":
        window = webview.windows[0]
        result = window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return None
        return result[0]


def _run_server() -> None:
    global _uvicorn_server, _startup_error
    try:
        import server  # 起動時のDBスキーマ初期化・バージョンチェックはserver側のstartupイベントで実施

        uv_config = uvicorn.Config(server.app, host="127.0.0.1", port=PORT, log_level="warning")
        _uvicorn_server = uvicorn.Server(uv_config)
        _uvicorn_server.run()
    except Exception:
        # importエラー(依存パッケージ不足等)やstartupイベントの例外はこのスレッド内で
        # 完結してしまい、メイン画面には「サーバーの起動に失敗しました」としか出せない。
        # 原因を後から確認できるよう、必ずファイルに残す。
        _startup_error = traceback.format_exc()
        try:
            config.ensure_dirs()
            with open(STARTUP_ERROR_LOG, "a", encoding="utf-8") as f:
                f.write(f"\n--- {datetime.now().isoformat(timespec='seconds')} ---\n")
                f.write(_startup_error)
        except OSError:
            pass


def _wait_for_server_ready(timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/status", timeout=1)
            return True
        except Exception:
            time.sleep(0.2)
    return False


def _show_error_window(message: str, detail: "str | None" = None) -> None:
    import html as html_module

    detail_html = ""
    if detail:
        detail_html = (
            "<p>詳細(" + html_module.escape(str(STARTUP_ERROR_LOG)) + " にも保存済み):</p>"
            f"<pre style='white-space:pre-wrap;background:#f0f0f0;padding:1em;"
            f"max-height:50vh;overflow:auto;font-size:0.8em'>{html_module.escape(detail)}</pre>"
        )
    html = f"""
    <html><head><meta charset="utf-8"><title>ミツカル - 起動エラー</title></head>
    <body style="font-family: sans-serif; padding: 2em;">
    <h2>起動できませんでした</h2>
    <p>{html_module.escape(message)}</p>
    {detail_html}
    </body></html>
    """
    webview.create_window("ミツカル - 起動エラー", html=html, width=800, height=600)
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
        if _startup_error:
            _show_error_window(
                "サーバーの起動に失敗しました。依存パッケージが正しくインストール"
                "されているか(pip install -r requirements.txt)を確認してください。",
                detail=_startup_error,
            )
        else:
            _show_error_window(
                "サーバーの起動に失敗しました(タイムアウト)。"
                "別のプロセスが同じポートを使用していないか確認してください。"
            )
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
