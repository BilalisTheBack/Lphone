#!/usr/bin/env bash
# LPhone Agent — macOS Build Scripti
# Kullanım: bash agent/build_macos.sh
# Çıktı: agent/dist/lphone (tek binary, Python gerektirmez)
# ─────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GREEN="\033[32m"; YELLOW="\033[33m"; CYAN="\033[36m"; RESET="\033[0m"
log()  { echo -e "${GREEN}[✓]${RESET} $*"; }
info() { echo -e "${CYAN}[→]${RESET} $*"; }
warn() { echo -e "${YELLOW}[!]${RESET} $*"; }

echo -e "${CYAN}LPhone Agent — macOS Binary Build${RESET}"
echo "──────────────────────────────────"

# Mimari
ARCH=$(uname -m)
info "Mimari: $ARCH"

# Python kontrolü (Homebrew önce, sistem Python sonra)
if command -v python3 &>/dev/null; then
    PYTHON=$(command -v python3)
elif command -v /opt/homebrew/bin/python3 &>/dev/null; then
    PYTHON=/opt/homebrew/bin/python3
else
    echo "python3 bulunamadı. Homebrew ile yükleyin:"
    echo "  brew install python@3.11"
    exit 1
fi
info "Python: $($PYTHON --version) at $PYTHON"

# Sanal ortam
if [ ! -d ".venv" ]; then
    info "Sanal ortam oluşturuluyor..."
    $PYTHON -m venv .venv
fi
source .venv/bin/activate

# Bağımlılıklar
info "Bağımlılıklar yükleniyor..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
pip install --quiet pyinstaller

# Temizlik
rm -rf build/ dist/

# Derleme
info "PyInstaller çalışıyor ($ARCH)..."
pyinstaller lphone.spec

# Code signing (isteğe bağlı — dağıtım için gerekli değil, yerel kullanımda sorun çıkarmaz)
if command -v codesign &>/dev/null; then
    info "Binary imzalanıyor (ad-hoc)..."
    codesign --force --deep --sign - dist/lphone 2>/dev/null || warn "İmzalama atlandı (gerekli değil)"
fi

# Quarantine bayrağını kaldır (macOS Gatekeeper uyarısını önler)
xattr -cr dist/lphone 2>/dev/null || true

BINARY="dist/lphone"
SIZE=$(du -sh "$BINARY" | cut -f1)
log "Binary oluşturuldu: $BINARY ($SIZE)"

echo ""
echo -e "${GREEN}Hazır!${RESET}"
echo ""
echo "Çalıştırmak için:"
echo "  chmod +x dist/lphone && ./dist/lphone"
echo ""
echo "  NOT: macOS'ta 'işlev tanımlı değil' uyarısı alırsanız:"
echo "  sudo xattr -rd com.apple.quarantine ./dist/lphone"
echo ""
echo "Tarayıcıdan: http://localhost:8000"
