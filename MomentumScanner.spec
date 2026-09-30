# PyInstaller spec - builds dist\MomentumScanner\MomentumScanner.exe
#
#   build_exe.bat          (recommended: builds and copies config/README next to the exe)
#   .venv\Scripts\python.exe -m PyInstaller MomentumScanner.spec --noconfirm
#
# "One folder" build: faster start and fewer antivirus false positives than one-file.
# config.yaml, .env and var\ are read from the folder the exe is in (see core/config.py).
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hidden = (collect_submodules("uvicorn") + collect_submodules("websockets")
          + collect_submodules("sqlalchemy.dialects.sqlite") + ["tzdata"])
datas = [("web/static", "web/static"), ("highs/study_snapshot.json", "highs")] + collect_data_files("tzdata")

a = Analysis(
    ["main.py"],
    pathex=["."],
    datas=datas,
    hiddenimports=hidden,
    excludes=["pandas", "pytest", "_pytest", "matplotlib", "tkinter", "IPython", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MomentumScanner",
    console=True,          # keep the console: it shows the live scan table and warnings
    icon=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="MomentumScanner")
