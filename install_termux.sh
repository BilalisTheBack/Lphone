#!/data/data/com.termux/files/usr/bin/bash
# LPhone Agent — Android/Termux Yükleyici
# Kullanım: curl -fsSL <raw_url>/agent/install_termux.sh | bash
# ──────────────────────────────────────────────────────────────

set -e

CYAN="\033[36m"; GREEN="\033[32m"; YELLOW="\033[33m"; RED="\033[31m"; RESET="\033[0m"

echo -e "${CYAN}"
echo " ╔═══════════════════════════════╗"
echo " ║   LPhone Agent — Termux       ║"
echo " ╚═══════════════════════════════╝"
echo -e "${RESET}"

log()  { echo -e "${GREEN}[✓]${RESET} $*"; }
warn() { echo -e "${YELLOW}[!]${RESET} $*"; }
die()  { echo -e "${RED}[✗]${RESET} $*"; exit 1; }

# Termux kontrolü
[ -d "/data/data/com.termux" ] || die "Bu script yalnızca Android Termux içinde çalışır."

# ── 1. Depoları güncelle ──────────────────────────────────────
log "Termux paket deposu güncelleniyor..."
pkg update -y -q && pkg upgrade -y -q

# ── 2. Temel araçlar ─────────────────────────────────────────
log "Temel araçlar kuruluyor..."
pkg install -y -q python git openssh termux-api 2>/dev/null || true

# ── 3. pip bağımlılıkları ────────────────────────────────────
log "Python bağımlılıkları kuruluyor..."
pip install --quiet --upgrade pip

# psutil Termux'ta derlemesi gerekebilir
pip install --quiet psutil 2>/dev/null || {
    warn "psutil pip ile kurulamadı, Termux paketi deneniyor..."
    pkg install -y -q python-psutil 2>/dev/null || true
    pip install --quiet psutil --no-build-isolation 2>/dev/null || true
}

pip install --quiet \
    fastapi \
    "uvicorn[standard]" \
    paramiko \
    pydantic \
    python-multipart \
    starlette

# ── 4. Kaynak kodu indir ─────────────────────────────────────
INSTALL_DIR="$HOME/lphone-agent"

# ─── Repo URL'yi ortam değişkeninden oku, ya da kullanıcıdan iste ────────────
if [ -z "${LPHONE_REPO:-}" ]; then
    echo ""
    echo -e "${YELLOW}GitHub repo URL'nizi girin (örn: https://github.com/kullanici/repo):${RESET}"
    read -r LPHONE_REPO
    LPHONE_REPO="${LPHONE_REPO%/}"   # sondaki / varsa kaldır
fi

if [ -z "$LPHONE_REPO" ]; then
    die "Repo URL'si boş. Lütfen LPHONE_REPO ortam değişkenini ayarlayın veya URL girin."
fi

if [ -d "$INSTALL_DIR/.git" ]; then
    log "Mevcut kurulum güncelleniyor..."
    cd "$INSTALL_DIR" && git pull -q
else
    log "Agent indiriliyor: $LPHONE_REPO"
    git clone -q "$LPHONE_REPO" "$INSTALL_DIR"
fi

# ── 5. Başlatma scripti oluştur ──────────────────────────────
cat > "$HOME/lphone.sh" <<'SCRIPT'
#!/data/data/com.termux/files/usr/bin/bash
cd "$HOME/lphone-agent/agent"
echo "LPhone Agent başlatılıyor → http://localhost:8000"
python app.py
SCRIPT
chmod +x "$HOME/lphone.sh"

# ── 6. Termux boot (isteğe bağlı) ───────────────────────────
BOOT_DIR="$HOME/.termux/boot"
mkdir -p "$BOOT_DIR"
cat > "$BOOT_DIR/lphone.sh" <<'BOOT'
#!/data/data/com.termux/files/usr/bin/bash
# Telefon yeniden başladığında otomatik başlat
termux-wake-lock
cd "$HOME/lphone-agent/agent"
python app.py &
BOOT
chmod +x "$BOOT_DIR/lphone.sh"
warn "Otomatik başlatma için Termux:Boot uygulamasını yükleyin (Play Store / F-Droid)."

# ── 7. Yerel IP göster ──────────────────────────────────────
LOCAL_IP=$(ip route get 1 2>/dev/null | awk '{print $7; exit}' || hostname -I 2>/dev/null | awk '{print $1}')

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════╗${RESET}"
echo -e "${GREEN}║   Kurulum tamamlandı!                    ║${RESET}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  Başlatmak için:   ${CYAN}~/lphone.sh${RESET}"
echo -e "  Bağlantı adresi:  ${CYAN}http://${LOCAL_IP:-<IP>}:8000${RESET}"
echo ""
echo -e "  Diğer cihazdan:   Aynı Wi-Fi ağında aynı adresi açın"
echo ""
