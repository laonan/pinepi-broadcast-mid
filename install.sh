#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# pinepi-broadcast-mid install script
# Installs the WebSocket broadcast service to /opt/pinepi-broadcast-mid and
# registers it as a systemd service.  Run as root on a Linux host.
# ---------------------------------------------------------------------------

SERVICE_NAME="pinepi-broadcast-mid"
SERVICE_USER="pinepi-broadcast-mid"
INSTALL_DIR="/opt/pinepi-broadcast-mid"
CONFIG_DIR="/etc/pinepi-broadcast-mid"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_SRC="${SCRIPT_DIR}/config.json"
CONFIG_DST="${CONFIG_DIR}/config.json"

# ---- Privilege check -------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Please run as root (sudo $0)" >&2
    exit 1
fi

# ---- System dependencies ---------------------------------------------------
echo "[1/6] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip

# ---- Install directory -----------------------------------------------------
echo "[2/6] Setting up ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"
cp "${SCRIPT_DIR}/python/broadcast_server_mid.py" "${INSTALL_DIR}/"

# Create dedicated user if it doesn't exist
if ! id "${SERVICE_USER}" &>/dev/null; then
    useradd --system --home-dir "${INSTALL_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

# ---- Python venv + packages ------------------------------------------------
echo "[3/6] Creating Python virtual environment..."
python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip
cp "${SCRIPT_DIR}/requirements.txt" "${INSTALL_DIR}/"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
"${INSTALL_DIR}/venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"

# ---- Config ----------------------------------------------------------------
echo "[4/6] Installing config.json..."

mkdir -p "${CONFIG_DIR}"

if [[ -f "${CONFIG_SRC}" ]]; then
    install -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 600 "${CONFIG_SRC}" "${CONFIG_DST}"
    echo "  -> Config installed from ${CONFIG_SRC} to ${CONFIG_DST}"
else
    echo "WARNING: config.json not found at ${CONFIG_SRC}" >&2
    install -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 600 "${SCRIPT_DIR}/config.json.example" "${CONFIG_DST}"
    echo "  -> Created ${CONFIG_DST} from config.json.example"
    echo ""
    echo "Please edit ${CONFIG_DST} and fill in your tokens, then restart the service:"
    echo "  sudo systemctl restart ${SERVICE_NAME}"
    echo ""
fi

if grep -q "REPLACE_WITH" "${CONFIG_DST}"; then
    echo ""
    echo "WARNING: config.json still contains placeholder values."
    echo "         Edit ${CONFIG_DST} with real tokens before the service will work."
    echo ""
    read -rp "Continue anyway? [y/N] " confirm
    [[ "${confirm,,}" == "y" ]] || exit 0
fi

# ---- Systemd service -------------------------------------------------------
echo "[5/6] Installing systemd service..."
cp "${SCRIPT_DIR}/pinepi-broadcast-mid.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

# ---- Start -----------------------------------------------------------------
echo "[6/6] Starting service..."
systemctl restart "${SERVICE_NAME}"
systemctl status "${SERVICE_NAME}" --no-pager

echo ""
echo "Done. Service '${SERVICE_NAME}' is running."
echo "  Logs:    journalctl -u ${SERVICE_NAME} -f"
echo "  Config:  ${CONFIG_DST}"
echo "  App dir: ${INSTALL_DIR}"
