#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${HOME}/.local/share/ios-photos/venv"
BIN_DIR="${HOME}/.local/bin"
APP_DIR="${HOME}/.local/share/applications"
ICON_DIR="${HOME}/.local/share/icons/hicolor"

echo "→ Erstelle/aktualisiere Python-Umgebung: ${VENV}"
python3 -m venv "${VENV}" --system-site-packages
"${VENV}/bin/pip" install --upgrade pip
"${VENV}/bin/pip" install "${ROOT}"

mkdir -p "${BIN_DIR}" "${APP_DIR}" \
  "${ICON_DIR}/scalable/apps" "${ICON_DIR}/symbolic/apps"

echo "→ Installiere Starterskript: ${BIN_DIR}/ios-photos"
cat > "${BIN_DIR}/ios-photos" <<EOF
#!/usr/bin/env bash
exec "${VENV}/bin/python" -m ios_photos "\$@"
EOF
chmod +x "${BIN_DIR}/ios-photos"

echo "→ Installiere Desktop-Dateien und Icons"
# Absoluter Pfad: ~/.local/bin fehlt oft im PATH der GNOME-Sitzung, sonst wird der Eintrag ignoriert
for f in io.github.ajuenemann.IosPhotos.desktop io.github.ajuenemann.IosPhotos.Monitor.desktop; do
  sed "s|^Exec=ios-photos|Exec=${BIN_DIR}/ios-photos|" "${ROOT}/ios_photos/data/${f}" > "${APP_DIR}/${f}"
done
cp "${ROOT}/ios_photos/data/icons/hicolor/scalable/apps/io.github.ajuenemann.IosPhotos-rosette.svg" \
  "${ICON_DIR}/scalable/apps/"
cp "${ROOT}/ios_photos/data/icons/hicolor/symbolic/apps/ios-photos-live-symbolic.svg" \
  "${ICON_DIR}/symbolic/apps/"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "${APP_DIR}" || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t -q "${HOME}/.local/share/icons/hicolor"
fi

echo
echo "Fertig. Starte die App mit:"
echo "  ios-photos"
echo
echo "Hinweis (optional für integrierte Video-Vorschau):"
echo "  sudo apt install gstreamer1.0-libav"
