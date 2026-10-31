#!/usr/bin/env bash
set -euo pipefail

# The same entry point installs, upgrades and repairs the service. Prepare a
# complete release before switching systemd away from the running application.
SERVICE_NAME="pinepi-broadcast-mid"
SERVICE_USER="pinepi-broadcast-mid"
INSTALL_DIR="/opt/pinepi-broadcast-mid"
CONFIG_DIR="/etc/pinepi-broadcast-mid"
STATE_DIR="/var/lib/pinepi-broadcast-mid"
UNIT_DIR="/etc/systemd/system"
WEB_DIR="/var/www/pinepi-broadcast-mid"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN=""

usage() {
    echo "Usage: sudo bash install.sh [--python /path/to/python3.11-3.13] [--web-dir /dedicated/web/root]"
}
while [[ $# -gt 0 ]]; do
    case "$1" in
        --python|--web-dir)
            [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
            if [[ "$1" == --python ]]; then PYTHON_BIN="$2"; else WEB_DIR="$2"; fi
            shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Run with sudo bash install.sh" >&2
    exit 1
fi
umask 022
[[ "$(uname -s)" == Linux ]] || { echo "ERROR: Debian/Ubuntu with systemd is required." >&2; exit 1; }
while [[ "$WEB_DIR" != / && "$WEB_DIR" == */ ]]; do WEB_DIR="${WEB_DIR%/}"; done
[[ "$WEB_DIR" == /* && "$WEB_DIR" != / && "$WEB_DIR" != //* && "$WEB_DIR" != */../* && "$WEB_DIR" != */.. && "$WEB_DIR" != */./* && "$WEB_DIR" != */. ]] || { echo "ERROR: --web-dir must be a dedicated absolute directory." >&2; exit 2; }
for protected_dir in "$INSTALL_DIR" "$CONFIG_DIR" "$STATE_DIR" "$UNIT_DIR" "$SCRIPT_DIR"; do
    if [[ "$WEB_DIR" == "$protected_dir" || "$WEB_DIR" == "$protected_dir/"* || "$protected_dir" == "$WEB_DIR/"* ]]; then
        echo "ERROR: --web-dir must not overlap application, configuration, state, unit or source directories." >&2
        exit 2
    fi
done
for command_name in apt-get systemctl flock; do
    command -v "$command_name" >/dev/null || { echo "ERROR: Missing $command_name (Debian/Ubuntu with systemd required)." >&2; exit 1; }
done
for source_file in python/broadcast_server_mid.py python/media_store.py python/identities.py python/deploy_check.py requirements.txt config.json.example pinepi-broadcast-mid.service examples/web/index.html examples/web/message-protocol.js; do
    [[ -f "$SCRIPT_DIR/$source_file" ]] || { echo "ERROR: Missing $source_file. Run install.sh from a complete git clone." >&2; exit 1; }
done

install -d -m 755 "$INSTALL_DIR"
# Prevent overlapping installation runs, including while packages are installed.
exec 9>"$INSTALL_DIR/.install.lock"
flock -n 9 || { echo "ERROR: Another installation is running." >&2; exit 1; }
CONFIG_DST="$CONFIG_DIR/config.json"
install -d -m 755 "$CONFIG_DIR"
if [[ -f "$CONFIG_DST" ]]; then
    echo "Preserving existing configuration: $CONFIG_DST (source config.json is ignored)."
elif [[ -f "$SCRIPT_DIR/config.json" ]]; then
    install -m 600 "$SCRIPT_DIR/config.json" "$CONFIG_DST"
else
    install -m 600 "$SCRIPT_DIR/config.json.example" "$CONFIG_DST"
    echo "Created $CONFIG_DST from the example. Fill in credentials, HTTPS origin and signing key, then rerun this same command." >&2
    exit 2
fi
# Never offer to start with placeholder credentials; no interactive prompts.
if grep -q 'REPLACE_WITH' "$CONFIG_DST"; then
    echo "ERROR: $CONFIG_DST contains placeholders. Edit it and rerun sudo bash install.sh; the current service was not changed." >&2
    exit 2
fi

# Avoid apt/network work when no compatible interpreter is available. The
# default Ubuntu Python may be too old (3.10) or too new for the pinned packages.
if [[ -n "$PYTHON_BIN" ]]; then
    candidates=("$PYTHON_BIN")
else
    candidates=(python3 python3.13 python3.12 python3.11)
fi
for candidate in "${candidates[@]}"; do
    if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys; sys.exit(not ((3,11) <= sys.version_info[:2] <= (3,13)))'; then
        PYTHON_BIN="$(command -v "$candidate")"
        break
    fi
    PYTHON_BIN=""
done
[[ -n "$PYTHON_BIN" ]] || { echo "ERROR: Install Python 3.11–3.13 and its matching venv package, then rerun with --python /path/to/python." >&2; exit 1; }

# Identity schema has no third-party dependencies. Reject outdated/ambiguous
# configuration before apt/pip work; full startup is checked in the new venv.
"$PYTHON_BIN" - "$SCRIPT_DIR/python" "$CONFIG_DST" <<'PYCONFIG'
import json
import sys
sys.path.insert(0, sys.argv[1])
from identities import Identities, unique_object
try:
    with open(sys.argv[2], encoding="utf-8") as stream:
        Identities(json.load(stream, object_pairs_hook=unique_object))
except (RuntimeError, ValueError, OSError) as error:
    print(f"ERROR: {error}. Update the identity sections in {sys.argv[2]} using config.json.example.", file=sys.stderr)
    raise SystemExit(1)
PYCONFIG

# A release keeps its permanent path, including its virtualenv: moving a venv
# after installation would invalidate scripts containing absolute shebangs.
RELEASE_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
RELEASE="$INSTALL_DIR/releases/$RELEASE_ID"
CURRENT="$INSTALL_DIR/current"
UNIT_DST="$UNIT_DIR/$SERVICE_NAME.service"
WEB_BACKUP="${WEB_DIR}.before-${RELEASE_ID}"
PREVIOUS_CURRENT=""
HAD_UNIT=0
WAS_ACTIVE=0
PROMOTED=0
SUCCESS=0

finish() {
    result=$?
    trap - EXIT
    if [[ "$result" -ne 0 && "$PROMOTED" -eq 1 && "$SUCCESS" -eq 0 ]]; then
        set +e
        echo "ERROR: Deployment failed; restoring the previous service and web files." >&2
        journalctl -u "$SERVICE_NAME" -n 40 --no-pager >&2
        systemctl stop "$SERVICE_NAME"
        if [[ -n "$PREVIOUS_CURRENT" ]]; then
            ln -s "$PREVIOUS_CURRENT" "$INSTALL_DIR/.current-rollback-$RELEASE_ID"
            mv -Tf "$INSTALL_DIR/.current-rollback-$RELEASE_ID" "$CURRENT"
        else
            [[ ! -L "$CURRENT" ]] || unlink "$CURRENT"
        fi
        if [[ -L "$WEB_DIR" && "$(readlink "$WEB_DIR")" == "$RELEASE/web" ]]; then unlink "$WEB_DIR"; fi
        if [[ -e "$WEB_BACKUP" || -L "$WEB_BACKUP" ]]; then mv -T "$WEB_BACKUP" "$WEB_DIR"; fi
        if [[ "$HAD_UNIT" -eq 1 ]]; then
            install -m 644 "$RELEASE/previous.service" "$UNIT_DST"
        else
            systemctl disable "$SERVICE_NAME"
            [[ ! -f "$UNIT_DST" ]] || unlink "$UNIT_DST"
        fi
        systemctl daemon-reload
        if [[ "$WAS_ACTIVE" -eq 1 ]]; then
            if ! systemctl restart "$SERVICE_NAME"; then echo "ERROR: Previous service could not restart; check journalctl." >&2; fi
        fi
    fi
    if [[ "$result" -ne 0 ]]; then
        echo "Installation did not complete. Fix the reported error and rerun sudo bash install.sh." >&2
        [[ -z "$RELEASE" ]] || echo "Prepared release kept for inspection: $RELEASE" >&2
    fi
    exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ---- Prepare; do not stop the old service during dependency downloads ------
echo "[1/6] Installing system dependencies..."
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
apt-get update -qq
venv_packages=(ffmpeg)
if ! "$PYTHON_BIN" -c 'import ensurepip' >/dev/null 2>&1; then
    python_minor="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    venv_packages+=("python${python_minor}-venv")
fi
apt-get install -y -qq "${venv_packages[@]}"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DST"
chmod 600 "$CONFIG_DST"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 700 "$STATE_DIR"
install -d -m 755 "$INSTALL_DIR/releases" "$RELEASE" "$RELEASE/web"

echo "[2/6] Preparing application and web files: $RELEASE"
install -m 644 "$SCRIPT_DIR"/python/*.py "$RELEASE/"
install -m 644 "$SCRIPT_DIR/requirements.txt" "$RELEASE/requirements.txt"
cp -R "$SCRIPT_DIR/examples/web/." "$RELEASE/web/"
# Nginx must be able to traverse/read the static files; no configuration or
# credentials are copied into this web-only directory.
find "$RELEASE/web" -type d -exec chmod 755 {} +
find "$RELEASE/web" -type f -exec chmod 644 {} +

echo "[3/6] Creating a fresh virtualenv and verifying dependencies..."
"$PYTHON_BIN" -m venv "$RELEASE/venv"
"$RELEASE/venv/bin/python" -m pip install -r "$RELEASE/requirements.txt"
"$RELEASE/venv/bin/python" -m pip check
"$RELEASE/venv/bin/python" -c 'import uvicorn, fastapi, jwt, PIL, python_multipart; print("Runtime dependencies OK")'

echo "[4/6] Validating configuration as the service user..."
runuser -u "$SERVICE_USER" -- "$RELEASE/venv/bin/python" "$RELEASE/deploy_check.py" validate --config "$CONFIG_DST"
if [[ -L "$CURRENT" ]]; then
    PREVIOUS_CURRENT="$(readlink "$CURRENT")"
elif [[ -e "$CURRENT" ]]; then
    echo "ERROR: $CURRENT must be absent or an installer-managed symlink." >&2
    exit 1
fi
if [[ -f "$UNIT_DST" ]]; then
    HAD_UNIT=1
    install -m 644 "$UNIT_DST" "$RELEASE/previous.service"
fi
if systemctl is-active --quiet "$SERVICE_NAME"; then WAS_ACTIVE=1; fi
install -d -m 755 "$(dirname "$WEB_DIR")" "$UNIT_DIR"

echo "[5/6] Switching application, web files and systemd unit..."
PROMOTED=1
if [[ "$HAD_UNIT" -eq 1 || "$WAS_ACTIVE" -eq 1 ]]; then systemctl stop "$SERVICE_NAME"; fi
ln -s "$RELEASE" "$INSTALL_DIR/.current-$RELEASE_ID"
mv -Tf "$INSTALL_DIR/.current-$RELEASE_ID" "$CURRENT"
if [[ -e "$WEB_DIR" || -L "$WEB_DIR" ]]; then mv -T "$WEB_DIR" "$WEB_BACKUP"; fi
ln -s "$RELEASE/web" "$WEB_DIR"
install -m 644 "$SCRIPT_DIR/pinepi-broadcast-mid.service" "$UNIT_DST"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl reset-failed "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo "[6/6] Waiting for the authenticated API to become ready..."
"$RELEASE/venv/bin/python" "$RELEASE/deploy_check.py" health
systemctl is-active --quiet "$SERVICE_NAME"
SUCCESS=1

echo ""
echo "Done. Service is running and local POST /api/send returns the expected 401."
echo "  Configuration preserved: $CONFIG_DST"
echo "  Active release:          $RELEASE"
echo "  Persistent data:         $STATE_DIR"
echo "  Web files:               $WEB_DIR"
echo "  Logs:                    journalctl -u $SERVICE_NAME -n 80 --no-pager"
echo "  Previous releases/web backups are retained; media data is never replaced."
echo ""
echo "Nginx TLS/domain settings are not modified. Keep your existing root/alias pointed at $WEB_DIR, or add:"
echo "  location /demo/ { alias $WEB_DIR/; index index.html; }"
echo "  Keep the API/WebSocket/media proxy rules from docs/media.md."
