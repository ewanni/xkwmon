#!/bin/sh
# Install xkwmon (XKW USB display driver) as a systemd service.
#
#   sudo ./install.sh              # run the service as root (default)
#   sudo ./install.sh --user       # create the "xkwmon" system user and run unprivileged
#   sudo ./install.sh --no-start   # install only
#   sudo PREFIX=/usr/local/lib/xkwmon ./install.sh
#
# Uninstall with: sudo ./uninstall.sh
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
PREFIX="${PREFIX:-/opt/xkwmon}"
CONFIG_DIR="${CONFIG_DIR:-/etc/xkwmon}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
UDEV_DIR="${UDEV_DIR:-/etc/udev/rules.d}"
SERVICE="xkwmon.service"

SERVICE_USER="root"
SERVICE_GROUP="root"
PANEL_GROUP="xkwmon"
START=1
AS_USER=0

while [ $# -gt 0 ]; do
    case "$1" in
        --user) AS_USER=1 ;;
        --no-start) START=0 ;;
        -h|--help)
            echo "usage: $0 [--user] [--no-start]"
            echo "  --user      run the service as the unprivileged 'xkwmon' user"
            echo "              (creates the user/group and the udev rule)"
            echo "  --no-start  install files but do not enable/start the service"
            exit 0
            ;;
        *)
            echo "unknown option: $1" >&2
            exit 2
            ;;
    esac
    shift
done

if [ "$(id -u)" -ne 0 ]; then
    echo "error: run as root (sudo $0)" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 is required" >&2
    exit 1
fi

# The udev rule always uses this group; it is created either way so the rule
# is valid. Only --user actually runs the service with it.
if ! getent group "$PANEL_GROUP" >/dev/null 2>&1; then
    echo ">> creating group $PANEL_GROUP"
    groupadd --system "$PANEL_GROUP"
fi

if [ "$AS_USER" -eq 1 ]; then
    SERVICE_USER="$PANEL_GROUP"
    SERVICE_GROUP="$PANEL_GROUP"
    if ! getent passwd "$SERVICE_USER" >/dev/null 2>&1; then
        echo ">> creating system user $SERVICE_USER"
        useradd --system --gid "$SERVICE_GROUP" --no-create-home \
                --home-dir /nonexistent --shell /usr/sbin/nologin "$SERVICE_USER"
    fi
    # /sys and /proc sensor files are world readable on Linux, and NVIDIA
    # device nodes are 0666, so no extra groups are required.
fi

echo ">> installing package to $PREFIX"
install -d "$PREFIX" "$CONFIG_DIR" "$BIN_DIR"
rm -rf "$PREFIX/xkwmon"
cp -a "$HERE/xkwmon" "$PREFIX/"
rm -rf "$PREFIX/xkwmon/__pycache__"
cp -a "$HERE/README.md" "$PREFIX/README.md" 2>/dev/null || true
cp -a "$HERE/config.example.ini" "$PREFIX/config.example.ini" 2>/dev/null || true

if [ ! -f "$CONFIG_DIR/config.ini" ]; then
    echo ">> creating $CONFIG_DIR/config.ini"
    install -m 644 "$HERE/config.example.ini" "$CONFIG_DIR/config.ini"
else
    echo ">> keeping existing $CONFIG_DIR/config.ini"
fi
chmod 644 "$CONFIG_DIR/config.ini"

echo ">> installing launcher to $BIN_DIR/xkwmon"
sed "s#@PREFIX@#$PREFIX#g" "$HERE/bin/xkwmon.in" > "$BIN_DIR/xkwmon"
chmod 755 "$BIN_DIR/xkwmon"

echo ">> installing systemd unit (User=$SERVICE_USER, Group=$SERVICE_GROUP)"
sed -e "s#@USER@#$SERVICE_USER#g" -e "s#@GROUP@#$SERVICE_GROUP#g" \
    "$HERE/systemd/$SERVICE" > "$UNIT_DIR/$SERVICE"
chmod 644 "$UNIT_DIR/$SERVICE"

echo ">> installing udev rule (panel group: $PANEL_GROUP)"
sed "s#@GROUP@#$PANEL_GROUP#g" \
    "$HERE/systemd/99-xkw-panel.rules" > "$UDEV_DIR/99-xkw-panel.rules"
chmod 644 "$UDEV_DIR/99-xkw-panel.rules"
udevadm control --reload 2>/dev/null || true
udevadm trigger --subsystem-match=hidraw 2>/dev/null || true

systemctl daemon-reload

if [ "$START" -eq 1 ]; then
    echo ">> enabling and (re)starting $SERVICE"
    systemctl enable "$SERVICE"
    systemctl restart "$SERVICE"
    sleep 2
    systemctl status --no-pager "$SERVICE" || true
else
    echo ">> not starting (--no-start). Start later with: systemctl enable --now $SERVICE"
fi

echo
echo ">> panel devices:"
"$BIN_DIR/xkwmon" devices || true
echo
echo ">> current sensors:"
"$BIN_DIR/xkwmon" probe || true
echo
echo "done. logs: journalctl -u $SERVICE -f"
echo "udp test:  xkwmon query --host 127.0.0.1 --cmd rundata"
