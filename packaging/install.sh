#!/bin/sh
# Установка агента x_connector с флешки. Без сети: ни apt, ни pip, ни curl.
# Запуск на сервере: sudo sh install.sh
set -eu

PREFIX="${PREFIX:-/opt/x_connector}"
ENV_CAPTURE="${XCONN_CAPTURE:-}"
ENV_PLAYBACK="${XCONN_PLAYBACK:-}"
detect_analog_pcm() {
    if [ ! -r /proc/asound/pcm ]; then
        echo "plughw:0,0"
        return
    fi
    line=$(grep -i analog /proc/asound/pcm | head -n 1 || true)
    if [ -z "$line" ]; then
        echo "plughw:0,0"
        return
    fi
    card=${line%%-*}
    rest=${line#*-}
    dev=${rest%%:*}
    card=$(echo "$card" | sed 's/^0*//')
    dev=$(echo "$dev" | sed 's/^0*//')
    [ -z "$card" ] && card=0
    [ -z "$dev" ] && dev=0
    echo "plughw:${card},${dev}"
}

CAPTURE=$(detect_analog_pcm)
PLAYBACK="$CAPTURE"

if [ "$(id -u)" -ne 0 ]; then
    echo "нужен root: sudo sh $0" >&2
    exit 1
fi

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ ! -d "$HERE/xconn_channel" ]; then
    echo "на флешке нет каталога xconn_channel: $HERE" >&2
    exit 1
fi

arch=$(uname -m)
case "$arch" in
    x86_64|amd64) py_key=x86_64 ;;
    aarch64|arm64) py_key=aarch64 ;;
    *) py_key= ;;
esac

have_system_python() {
    command -v python3 >/dev/null 2>&1
}

install_bundled_python() {
    if [ -z "$py_key" ]; then
        return 1
    fi
    if [ ! -d "$HERE/python-linux" ]; then
        return 1
    fi
    set -- "$HERE/python-linux/"*"${py_key}"*install_only_stripped.tar.gz
    if [ ! -e "$1" ]; then
        return 1
    fi
    echo "ставлю встроенный python3 ($arch) в $PREFIX/python"
    mkdir -p "$PREFIX"
    rm -rf "$PREFIX/python"
    tar -xzf "$1" -C "$PREFIX"
    if [ ! -x "$PREFIX/python/bin/python3" ]; then
        echo "архив python3 распаковался без bin/python3" >&2
        return 1
    fi
    return 0
}

install_alsa_debs() {
    if command -v aplay >/dev/null 2>&1 && command -v arecord >/dev/null 2>&1; then
        return 0
    fi
    code=""
    if [ -f /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        code="${VERSION_CODENAME:-}"
    fi
    if [ -z "$code" ] || [ ! -d "$HERE/alsa-debs/$code" ]; then
        for fallback in resolute noble jammy; do
            if [ -d "$HERE/alsa-debs/$fallback" ]; then
                echo "alsa-debs/$code нет, пробую $fallback"
                code=$fallback
                break
            fi
        done
    fi
    if [ -z "$code" ] || [ ! -d "$HERE/alsa-debs/$code" ]; then
        echo "aplay/arecord нет, на флешке нет alsa-debs/$code" >&2
        echo "на ноутбуке: py -m xconn_channel stick" >&2
        return 1
    fi
    set -- "$HERE/alsa-debs/$code"/*.deb
    if [ ! -e "$1" ]; then
        echo "каталог alsa-debs/$code пуст" >&2
        return 1
    fi
    echo "ставлю alsa-utils с флешки ($code, dpkg -i, без apt)"
    dpkg -i "$@"
}

install_python_debs() {
    if [ ! -d "$HERE/python-debs" ]; then
        return 1
    fi
    set -- "$HERE/python-debs"/*.deb
    if [ ! -e "$1" ]; then
        return 1
    fi
    echo "python3 нет, ставлю пакеты с флешки (dpkg -i, без apt)"
    dpkg -i "$@"
}

if ! install_bundled_python; then
    if have_system_python; then
        :
    elif install_python_debs; then
        :
    fi
fi

if [ -x "$PREFIX/python/bin/python3" ]; then
    PYTHON="$PREFIX/python/bin/python3"
elif have_system_python; then
    PYTHON=python3
else
    echo "на сервере нет python3 и на флешке нет архива под $arch." >&2
    echo "на ноутбуке: py -m xconn_channel stick <буква>:  (нужен интернет)" >&2
    exit 1
fi

if ! install_alsa_debs; then
    if command -v aplay >/dev/null 2>&1 && command -v arecord >/dev/null 2>&1; then
        :
    else
        echo "на сервере нет aplay/arecord. без них агент не откроет звуковую карту." >&2
        exit 1
    fi
fi

if [ -f /etc/default/xconn-agent ]; then
    set -a
    # shellcheck disable=SC1091
    . /etc/default/xconn-agent
    set +a
    CAPTURE="${XCONN_CAPTURE:-$CAPTURE}"
    PLAYBACK="${XCONN_PLAYBACK:-$PLAYBACK}"
fi
if [ -n "$ENV_CAPTURE" ]; then
    CAPTURE="$ENV_CAPTURE"
fi
if [ -n "$ENV_PLAYBACK" ]; then
    PLAYBACK="$ENV_PLAYBACK"
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

# Аварийная консоль чинит сеть: пароль в PTY завис бы на пол-обмена.
# Физический доступ к кабелю = тот же человек, что у машины (AGENTS.md 3.6).
# !requiretty не писать: sudo 1.9.17+ (Ubuntu 26.04) его больше не знает,
# visudo -cf валит весь install.sh.
sudoers=/etc/sudoers.d/xconn-agent
cat > "$sudoers" <<'EOF'
xconn ALL=(root) NOPASSWD:ALL
EOF
chmod 440 "$sudoers"
if command -v visudo >/dev/null 2>&1; then
    if ! visudo -cf "$sudoers"; then
        rm -f "$sudoers"
        echo "sudoers для xconn не принят, visudo -cf не прошёл" >&2
        exit 1
    fi
fi

cat > /etc/default/xconn-agent <<EOF
XCONN_PREFIX="$PREFIX"
XCONN_CAPTURE="$CAPTURE"
XCONN_PLAYBACK="$PLAYBACK"
PYTHONPATH="$PREFIX"
EOF

if [ -d /etc/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
    sed "s|/opt/x_connector|$PREFIX|g" "$HERE/xconn-agent.service" > /etc/systemd/system/xconn-agent.service
    systemctl daemon-reload
    systemctl enable xconn-agent.service
    echo "агент в $PREFIX, unit включён."
    echo "кабели, затем: systemctl start xconn-agent"
    echo "устройства ALSA: arecord -l && aplay -l"
    echo "потом правьте XCONN_CAPTURE / XCONN_PLAYBACK в /etc/default/xconn-agent"
else
    echo "systemd нет — запуск: $PREFIX/run-agent.sh"
fi

PYTHONPATH="$PREFIX" "$PYTHON" -c "import xconn_channel; print('xconn_channel', xconn_channel.__version__)"
