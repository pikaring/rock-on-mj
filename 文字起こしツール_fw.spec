# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_data_files, collect_submodules

binaries = (
    collect_dynamic_libs('ctranslate2')
    + collect_dynamic_libs('onnxruntime')
    + collect_dynamic_libs('av')
)
datas = (collect_data_files('faster_whisper') + collect_data_files('docx'))
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
    name='mojiokoshi-fw',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    console=False,
    version='version_info.txt',   # 発行元・製品名（誤検知を減らすため）
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
    upx=False,
    upx_exclude=[],
    name='mojiokoshi-fw',
)
