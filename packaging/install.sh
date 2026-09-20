#!/bin/sh
# Установка агента x_connector с флешки. Без сети: ни apt, ни pip, ни curl.
# Запуск на сервере: sudo sh install.sh
set -eu

PREFIX="${PREFIX:-/opt/x_connector}"
CAPTURE="${XCONN_CAPTURE:-hw:0,0}"
PLAYBACK="${XCONN_PLAYBACK:-hw:0,0}"

if [ "$(id -u)" -ne 0 ]; then
    echo "нужен root: sudo sh $0" >&2
    exit 1
fi

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ ! -d "$HERE/xconn_channel" ]; then
    echo "на флешке нет каталога xconn_channel: $HERE" >&2
    exit 1
fi

have_python() {
    command -v python3 >/dev/null 2>&1
}

if have_python; then
    :
elif [ -d "$HERE/python-debs" ]; then
    set -- "$HERE/python-debs"/*.deb
    if [ -e "$1" ]; then
        echo "python3 нет, ставлю пакеты с флешки (dpkg -i, без apt)"
        dpkg -i "$@"
    fi
fi

if ! have_python; then
    echo "на сервере нет python3." >&2
    echo "на ноутбуке скачайте .deb в python-debs/ на этой флешке и повторите." >&2
    exit 1
fi

mkdir -p "$PREFIX"
rm -rf "$PREFIX/xconn_channel"
cp -a "$HERE/xconn_channel" "$PREFIX/xconn_channel"
cp "$HERE/run-agent.sh" "$PREFIX/run-agent.sh"
chmod 755 "$PREFIX/run-agent.sh"

if ! id xconn >/dev/null 2>&1; then
    if getent group audio >/dev/null 2>&1; then
        useradd --system --home-dir "$PREFIX" --shell /usr/sbin/nologin -g audio xconn
    else
        useradd --system --home-dir "$PREFIX" --shell /usr/sbin/nologin xconn
    fi
fi
if getent group audio >/dev/null 2>&1; then
    usermod -aG audio xconn 2>/dev/null || true
fi
if getent group audio >/dev/null 2>&1; then
    chown -R xconn:audio "$PREFIX"
else
    chown -R xconn "$PREFIX"
fi

cat > /etc/default/xconn-agent <<EOF
XCONN_CAPTURE=$CAPTURE
XCONN_PLAYBACK=$PLAYBACK
PYTHONPATH=$PREFIX
EOF

if [ -d /etc/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
    cp "$HERE/xconn-agent.service" /etc/systemd/system/xconn-agent.service
    systemctl daemon-reload
    systemctl enable xconn-agent.service
    echo "агент в $PREFIX, unit включён."
    echo "кабели, затем: systemctl start xconn-agent"
    echo "устройства ALSA: arecord -l && aplay -l"
    echo "потом правьте XCONN_CAPTURE / XCONN_PLAYBACK в /etc/default/xconn-agent"
else
    echo "systemd нет — запуск: $PREFIX/run-agent.sh"
fi

PYTHONPATH="$PREFIX" python3 -c "import xconn_channel; print('xconn_channel', xconn_channel.__version__)"
