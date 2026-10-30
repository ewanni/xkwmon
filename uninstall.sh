#!/bin/sh
# Remove xkwmon installed by install.sh.
#
#   sudo ./uninstall.sh            # remove service/files, keep config
#   sudo ./uninstall.sh --purge    # also remove the xkwmon user/group and config
set -eu

PREFIX="${PREFIX:-/opt/xkwmon}"
CONFIG_DIR="${CONFIG_DIR:-/etc/xkwmon}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
UDEV_DIR="${UDEV_DIR:-/etc/udev/rules.d}"
SERVICE="xkwmon.service"
PURGE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --purge) PURGE=1 ;;
        -h|--help)
            echo "usage: $0 [--purge]"
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

echo ">> stopping and disabling $SERVICE"
systemctl disable --now "$SERVICE" 2>/dev/null || true
rm -f "$UNIT_DIR/$SERVICE"
systemctl daemon-reload || true

echo ">> removing udev rule"
rm -f "$UDEV_DIR/99-xkw-panel.rules"
udevadm control --reload 2>/dev/null || true

echo ">> removing files"
rm -f "$BIN_DIR/xkwmon"
rm -rf "$PREFIX"

if [ "$PURGE" -eq 1 ]; then
    echo ">> removing config and user/group"
    rm -rf "$CONFIG_DIR"
    if getent passwd xkwmon >/dev/null 2>&1; then
        userdel xkwmon 2>/dev/null || true
    fi
    if getent group xkwmon >/dev/null 2>&1; then
        groupdel xkwmon 2>/dev/null || true
    fi
else
    echo ">> config kept at $CONFIG_DIR/config.ini (use --purge to remove)"
fi

echo "done"
