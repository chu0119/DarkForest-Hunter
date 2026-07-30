# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 — DarkForest Hunter 单 exe 便携包
用法: pyinstaller DarkForestHunter.spec
生成: dist/DarkForestHunter.exe
"""
import os

block_cipher = None

a = Analysis(
    ['run.py'],
    pathex=[os.path.abspath('.')],
    binaries=[],
    datas=[],
    hiddenimports=[
        # 扫描器模块（动态导入需显式声明）
        'scanners.ai_platforms',
        'scanners.github_raw',
        'scanners.pastebin',
        'scanners.replicate',
        'scanners.reddit',
        'scanners.google_dork',
        'scanners.github_gist',
        'scanners.github_issues',
        'scanners.github_commits',
        'scanners.github_events',
        'scanners.gitlab',
        'scanners.gitee',
        'scanners.huggingface',
        'scanners.pypi',
        'scanners.npm_registry',
        'scanners.stackoverflow',
        'scanners.docker',
        'scanners.wayback',
        'scanners.commoncrawl',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 排除不需要的大模块，减小体积
        # 注意: email/html 等被 pkg_resources 间接依赖，不能排除
        'tkinter', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'shiboken2', 'shiboken6',
        'matplotlib', 'numpy', 'pandas', 'scipy', 'PIL', 'cv2',
        'IPython', 'notebook', 'jupyter', 'pytest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='DarkForestHunter',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
