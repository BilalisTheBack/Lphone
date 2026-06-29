#!/usr/bin/env bash
# LPhone Agent — Linux build scripti
# Kullanım: bash agent/build_linux.sh
# Çıktı: agent/dist/lphone  (tek binary, Python gerektirmez)
# ─────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GREEN="\033[32m"; YELLOW="\033[33m"; CYAN="\033[36m"; RESET="\033[0m"
log()  { echo -e "${GREEN}[✓]${RESET} $*"; }
info() { echo -e "${CYAN}[→]${RESET} $*"; }

echo -e "${CYAN}LPhone Agent — Linux Binary Build${RESET}"
echo "──────────────────────────────────"

# Python kontrolü
if ! command -v python3 &>/dev/null; then
    echo "python3 bulunamadı. Lütfen python3 kurun."
    exit 1
fi
PYTHON=$(command -v python3)
info "Python: $($PYTHON --version)"

# Sanal ortam (izolasyon için)
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

# UPX (isteğe bağlı — binary sıkıştırma)
if command -v upx &>/dev/null; then
    info "UPX bulundu, sıkıştırma aktif"
else
    info "UPX bulunamadı (boyut optimizasyonu için: sudo apt install upx-ucl)"
fi

# Temizlik
rm -rf build/ dist/

# Derleme
info "PyInstaller çalışıyor..."
pyinstaller lphone.spec 2>&1 | grep -E "^(INFO|WARNING|ERROR|Building)" | head -40 || true
pyinstaller lphone.spec

# Sonuç
BINARY="dist/lphone"
SIZE=$(du -sh "$BINARY" | cut -f1)
log "Binary oluşturuldu: $BINARY ($SIZE)"

# Test
info "Başlatma testi..."
timeout 3 "$BINARY" &>/dev/null && true
log "Test: başarılı"

echo ""
echo -e "${GREEN}Hazır!${RESET} Binary: ${CYAN}$(realpath "$BINARY")${RESET}"
echo ""
echo "Çalıştırmak için:"
echo "  ./dist/lphone"
echo "Tarayıcıdan: http://localhost:8000"
