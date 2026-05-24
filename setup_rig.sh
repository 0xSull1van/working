#!/usr/bin/env bash
# setup_rig.sh - bootstrap a fresh Ubuntu rig (or hub) for Pearl/AlphaPool.
#
# Usage on each server:
#   git clone https://github.com/0xSull1van/working.git okak && cd okak
#   PRL_ADDRESS=prl1pYOURADDRESS \
#   TG_BOT_TOKEN=123456:AAH... \
#   TG_CHAT_ID=123456789 \
#   ROLE=run \
#     bash setup_rig.sh
#
# ROLE values:
#   run    - just miner on this rig (TG creds optional). Use on the bulk of rigs.
#   serve  - miner + Telegram bot in one process. Use on ONE rig (your hub-rig).
#   hub    - monitor + Telegram bot, NO mining. Use on a VPS or your laptop.
#
# Optional env:
#   PRL_POOL          default stratum+tcp://eu1.alphapool.tech:5566 (us2 for US)
#   PRL_PRICE_USD     default 0.18, used by /earnings
#   PRL_FORCE_BACKEND default blackwell-native (RTX 5090). For 4090/4060Ti: ada.
#   PRL_MAX_TEMP_C    default 80
#   PRL_MAX_POWER_W   default 450 (5090). 350 for 4090.
set -euo pipefail

PRL_ADDRESS="${PRL_ADDRESS:-}"
TG_BOT_TOKEN="${TG_BOT_TOKEN:-}"
TG_CHAT_ID="${TG_CHAT_ID:-}"
ROLE="${ROLE:-run}"
POOL="${PRL_POOL:-stratum+tcp://eu1.alphapool.tech:5566}"
PRICE_USD="${PRL_PRICE_USD:-0.18}"
BACKEND="${PRL_FORCE_BACKEND:-blackwell-native}"
MAX_TEMP_C="${PRL_MAX_TEMP_C:-80}"
MAX_POWER_W="${PRL_MAX_POWER_W:-450}"

if [[ -z "$PRL_ADDRESS" ]]; then
    echo "ERROR: PRL_ADDRESS env var is required" >&2
    exit 1
fi
case "$ROLE" in
    run|serve|hub) ;;
    *) echo "ERROR: ROLE must be one of: run serve hub (got '$ROLE')" >&2; exit 1 ;;
esac
if [[ "$ROLE" == "serve" || "$ROLE" == "hub" ]]; then
    if [[ -z "$TG_BOT_TOKEN" || -z "$TG_CHAT_ID" ]]; then
        echo "ERROR: TG_BOT_TOKEN and TG_CHAT_ID required for ROLE=$ROLE" >&2
        exit 1
    fi
fi

REPO_DIR="$(pwd)"
echo ">> repo dir: $REPO_DIR"
if [[ ! -f "$REPO_DIR/prl_watch.py" ]]; then
    echo "ERROR: run this script from inside the cloned repo (where prl_watch.py lives)" >&2
    exit 1
fi

echo ">> apt deps"
if ! command -v python3 >/dev/null 2>&1 || ! command -v git >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo apt-get install -y python3 git curl
fi

if [[ "$ROLE" != "hub" ]]; then
    if ! command -v nvidia-smi >/dev/null 2>&1; then
        echo "WARN: nvidia-smi not found. Install NVIDIA driver 570+ before running the miner."
        echo "      sudo ubuntu-drivers install nvidia:570  &&  sudo reboot"
    fi
    if [[ ! -x "$REPO_DIR/alpha-miner" ]]; then
        echo ">> fetching alpha-miner"
        curl -fL -o "$REPO_DIR/alpha-miner" \
            https://github.com/AlphaMine-Tech/alpha-miner/releases/latest/download/alpha-miner
        chmod +x "$REPO_DIR/alpha-miner"
        echo ">> verifying checksum"
        ( cd "$REPO_DIR" && curl -fL \
            https://github.com/AlphaMine-Tech/alpha-miner/releases/latest/download/SHA256SUMS \
            | sha256sum -c --ignore-missing )
    fi
fi

echo ">> writing .env"
{
    echo "PRL_ADDRESS=$PRL_ADDRESS"
    echo "PRL_POOL=$POOL"
    echo "PRL_PRICE_USD=$PRICE_USD"
    if [[ -n "$TG_BOT_TOKEN" ]]; then
        echo "TG_BOT_TOKEN=$TG_BOT_TOKEN"
        echo "TG_CHAT_ID=$TG_CHAT_ID"
        echo "TG_STATUS_INTERVAL_MINUTES=60"
    fi
    if [[ "$ROLE" != "hub" ]]; then
        echo "PRL_MINER_BINARY=$REPO_DIR/alpha-miner"
        echo "PRL_FORCE_BACKEND=$BACKEND"
        echo "PRL_MAX_TEMP_C=$MAX_TEMP_C"
        echo "PRL_MAX_POWER_W=$MAX_POWER_W"
        echo "PRL_GPU_REPORT_INTERVAL=120"
    fi
} > "$REPO_DIR/.env"
chmod 600 "$REPO_DIR/.env"

SERVICE="prl-$ROLE"
echo ">> installing systemd unit /etc/systemd/system/$SERVICE.service"
sudo tee "/etc/systemd/system/$SERVICE.service" > /dev/null <<EOF
[Unit]
Description=PRL $ROLE (prl_watch.py)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$REPO_DIR
ExecStart=/usr/bin/python3 $REPO_DIR/prl_watch.py $ROLE
Restart=always
RestartSec=15
Nice=-5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE"
sleep 2
echo
echo "=== systemctl status $SERVICE ==="
sudo systemctl --no-pager status "$SERVICE" | head -12 || true
echo
echo "Tail logs:  journalctl -u $SERVICE -f"
echo "Stop:       sudo systemctl stop $SERVICE"
echo "Disable:    sudo systemctl disable --now $SERVICE"
