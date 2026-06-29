# LPhone Agent

Herhangi bir cihazdan (telefon, tablet, bilgisayar) tarayıcı aracılığıyla bir makineyi uzaktan yönetmenizi sağlayan tek dosyalık bir ajan.

## Özellikler

- **PTY Terminal** — gerçek bash oturumu, çoklu sekme
- **Dosya Yöneticisi** — yükleme, indirme, kopyalama, arşivleme
- **Syntax Editör** — Dracula temalı kod düzenleyici
- **Sistem Dashboard** — CPU/RAM/Disk anlık izleme
- **SSH / SFTP** — uzak sunuculara bağlantı
- **Ekran İzleme** — canlı ekran görüntüsü akışı
- **İşlem Yöneticisi** — süreçleri listele, sonlandır
- **Paket Yöneticisi** — apt, dnf, pacman, brew, winget desteği
- **Görev Zamanlayıcı** — periyodik komut çalıştırma
- **Pano Senkronizasyonu** — cihazlar arası pano paylaşımı

---

## Kurulum (Python gerektirmez)

Hazır binary'yi [Releases](../../releases) sayfasından indirin.

### Linux

```bash
chmod +x lphone-linux-x86_64
./lphone-linux-x86_64
```

### macOS

```bash
chmod +x lphone-macos-arm64   # Apple Silicon
./lphone-macos-arm64

# veya Intel Mac:
chmod +x lphone-macos-x86_64
./lphone-macos-x86_64
```

> **Not:** Gatekeeper uyarısı alırsanız:
> `sudo xattr -rd com.apple.quarantine ./lphone-macos-*`

### Windows

`lphone-windows-x86_64.exe` dosyasını çift tıklayın.
(Windows Defender uyarısı alırsanız → Daha fazla bilgi → Yine de çalıştır)

### Android (Termux)

```bash
# Termux'u F-Droid'den yükleyin (Play Store sürümü güncel değil)
# Sonra Termux içinde:
curl -fsSL https://raw.githubusercontent.com/BilalisTheBack/Lphone/main/agent/install_termux.sh | bash
```

---

## Ağ Erişimi

Agent başladığında `http://0.0.0.0:8000` adresinde dinler.

- **Aynı makineden:** `http://localhost:8000`
- **Aynı ağdaki başka cihazdan:** `http://<makinenin-IP-adresi>:8000`

IP adresini bulmak için:
- Linux/macOS: `hostname -I` veya `ip addr`
- Windows: `ipconfig`
- Termux: `ip addr show wlan0`

---

## Kaynaktan Derleme

Kendi binary'nizi derlemek için Python 3.11+ gereklidir.

```bash
cd agent
pip install -r requirements.txt pyinstaller

# Linux / macOS
bash build_linux.sh   # veya build_macos.sh

# Windows
build_windows.bat
```

Veya doğrudan kaynak koddan çalıştırın:

```bash
cd agent
pip install -r requirements.txt
python app.py
```

---

## GitHub Actions ile Otomatik Build

Yeni bir sürüm etiketi oluşturduğunuzda tüm platformlar için otomatik olarak binary derlenir:

```bash
git tag v1.0.0
git push origin v1.0.0
```

Workflow `.github/workflows/build.yml` dosyasında tanımlıdır ve şunları üretir:
- `lphone-linux-x86_64`
- `lphone-macos-arm64`
- `lphone-macos-x86_64`
- `lphone-windows-x86_64.exe`
