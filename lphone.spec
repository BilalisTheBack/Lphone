# -*- mode: python ; coding: utf-8 -*-
"""
LPhone Agent — PyInstaller build spec
Üretilen binary: dist/lphone  (Linux/macOS) veya dist/lphone.exe (Windows)
Tüm bağımlılıklar ve Python çalışma zamanı tek dosyaya gömülür.

Kullanım:
  pyinstaller lphone.spec          # tek klasör (daha hızlı)
  pyinstaller lphone.spec --onefile  # tek dosya
"""

import sys
import os

block_cipher = None

# ---------------------------------------------------------------------------
# Hidden imports — FastAPI / Uvicorn / Starlette modülleri PyInstaller
# tarafından otomatik tespit edilmez; elle bildirmek gerekir.
# ---------------------------------------------------------------------------
hidden_imports = [
    # Uvicorn — tüm dinamik import yollarını kapsamak gerekir;
    # frozen binary'de importlib.import_module ile yüklenenler otomatik
    # tespit edilmez.
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",         # dinamik loop seçimi
    "uvicorn.loops.asyncio",
    "uvicorn.loops.uvloop",       # uvloop varsa kullanılır
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "uvicorn._subprocess",
    "uvicorn.supervisors",
    "uvicorn.supervisors.basereload",
    "uvicorn.supervisors.statreload",
    "uvicorn.supervisors.watchfilesreload",
    "uvicorn.middleware",
    "uvicorn.middleware.asgi2",
    "uvicorn.middleware.message_logger",
    "uvicorn.middleware.proxy_headers",
    "uvicorn.middleware.wsgi",
    "uvicorn.config",
    "uvicorn.importer",
    "uvicorn.main",
    # FastAPI / Starlette
    "fastapi",
    "fastapi.routing",
    "fastapi.responses",
    "fastapi.staticfiles",
    "starlette",
    "starlette.routing",
    "starlette.applications",
    "starlette.responses",
    "starlette.requests",
    "starlette.websockets",
    "starlette.middleware",
    "starlette.middleware.cors",
    "starlette.background",
    "starlette.concurrency",
    "starlette.datastructures",
    "starlette.exceptions",
    "starlette.formparsers",
    "starlette.templating",
    "starlette.testclient",
    "starlette.types",
    # Pydantic v2
    "pydantic",
    "pydantic.v1",
    "pydantic_core",
    # HTTP / async
    "anyio",
    "anyio._backends._asyncio",
    "anyio._backends._trio",
    "anyio.streams.memory",
    "httptools",
    "websockets",
    "websockets.legacy",
    "websockets.legacy.server",
    "wsproto",
    "h11",
    # Parametrik / crypto (paramiko optional)
    "paramiko",
    "cryptography",
    "cryptography.hazmat",
    "cryptography.hazmat.bindings._rust",
    "Crypto",
    "nacl",
    # Sistem
    "psutil",
    "multipart",
    "python_multipart",
    # Email validator (pydantic isteğe bağlı)
    "email_validator",
]

# Windows'ta pty modülü yoktur; sadece Unix'te ekle
if sys.platform != "win32":
    hidden_imports += ["pty", "fcntl", "termios"]

# ---------------------------------------------------------------------------
a = Analysis(
    ["app.py"],
    pathex=[os.getcwd()],
    binaries=[],
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # platform uyumsuz modülleri dışarıda bırak
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "scipy",
              "PIL", "cv2", "torch", "tensorflow"],
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
    name="lphone",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,          # UPX sıkıştırma (boyutu ~30-40% azaltır)
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,      # terminal penceresi (agent arka planda log basar)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,  # None = build makinesinin mimarisi
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
