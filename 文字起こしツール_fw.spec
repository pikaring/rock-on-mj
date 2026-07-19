# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_data_files, collect_submodules

binaries = (
    collect_dynamic_libs('ctranslate2')
    + collect_dynamic_libs('onnxruntime')
    + collect_dynamic_libs('av')
)
datas = (collect_data_files('faster_whisper') + collect_data_files('docx')
         + [('rock_on_mj.ico', '.')])   # ウィンドウのアイコン用に同梱
hiddenimports = (
    ['ctranslate2', 'onnxruntime', 'av']
    + ['docx', 'openpyxl', 'et_xmlfile', 'lxml', 'lxml.etree', 'lxml._elementpath']
    + collect_submodules('faster_whisper')
    + collect_submodules('openpyxl')
)

a = Analysis(
    ['whisper_gui_fw.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'torchvision', 'torchaudio', 'whisper', 'tensorflow', 'matplotlib', 'scipy'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='文字起こしツール',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=False,
    icon='rock_on_mj.ico',
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='文字起こしツール',
)
