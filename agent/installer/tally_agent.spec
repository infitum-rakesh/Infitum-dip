# PyInstaller spec — builds a single tally_agent.exe bundling the
# agent/tally_agent package and its dependencies, so a customer's
# machine needs no Python install.
#
# Real spec file, but NOT built/run here — PyInstaller's Windows build
# (pulling in pywin32's DLLs) can only be produced on a Windows machine
# or Windows CI runner; neither exists in this project's environment
# (see agent/tally_agent/__init__.py's own disclosure). Invoke via
# build.ps1 on an actual Windows machine.
#
#   pyinstaller installer/tally_agent.spec
#
# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['../tally_agent/cli.py'],
    pathex=['..'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'win32timezone',        # pywin32's service framework imports this implicitly
        'tally_agent.windows_service',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='tally_agent',
    console=True,
    onefile=True,
    icon=None,
)
