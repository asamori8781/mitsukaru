# PyInstaller spec: 手動CLIコマンドを版管理し、Windows/macOS/Linuxいずれでも
# 同じ `pyinstaller mitsukaru.spec` 一発でビルドできるようにするための設定。
#
# 使い方:
#   pip install -r requirements.txt pyinstaller
#   pyinstaller mitsukaru.spec
#
# 生成物は dist/mitsukaru/ 配下(onedir)。data/ と logs/ は実行ファイルと
# 同階層に実行時生成されるため、dist/mitsukaru/ ごとコピーすればポータブルに
# 動作する(埋め込みモデルは同梱せず初回起動時にdata/models/へダウンロード)。
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
