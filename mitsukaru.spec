# PyInstaller spec: 手動CLIコマンドを版管理し、Windows/macOS/Linuxいずれでも
# 同じ `pyinstaller mitsukaru.spec` 一発でビルドできるようにするための設定。
#
# 使い方:
#   pip install -r requirements.txt pyinstaller
#   pyinstaller mitsukaru.spec
#
# 生成物は dist/mitsukaru/ 配下(onedir: mitsukaru.exe + _internalフォルダ)。
# onefile(単一exe)ではなくonedirを採用しているのは、onefileは起動のたびに
# 一時フォルダへ全内容を展開し終了時に削除する仕様のため、2回目以降の起動も
# 毎回展開コストがかかり続けるため(「初回だけ展開して以降は速くなる」機能
# ではない)。onedirはビルド時に1回展開済みの状態で配布されるため、
# dist/mitsukaru/ フォルダを配置した後は毎回の展開なしで常に高速起動する。
# data/ と logs/ は実行ファイルと同階層に実行時生成されるため、
# dist/mitsukaru/ フォルダごと配置すればポータブルに動作する(埋め込み
# モデルは同梱せず初回のコンテンツインデックス作成時にdata/models/へ
# ダウンロード)。
from PyInstaller.utils.hooks import collect_all

block_cipher = None

# onnxruntime/tokenizersはネイティブ拡張(.so/.pyd)とバンドルデータを含むため、
# 自動解析だけでは一部の共有ライブラリが取りこぼされることがある。
# collect_allで確実に同梱する(pypdf/python-docx/openpyxl/python-pptxは純Python
# なので通常のimport解析で十分)。
datas = []
binaries = []
hiddenimports = []
for pkg in ("onnxruntime", "tokenizers"):
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

datas.append(("static", "static"))

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pandas"],  # 誤って依存が紛れ込んでいないことをビルド時にも保証する
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="mitsukaru",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon="assets/icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="mitsukaru",
)
