#!/bin/sh
# Запуск агента. Читает /etc/default/xconn-agent, в сеть не ходит.
set -eu
PREFIX=${XCONN_PREFIX:-/opt/x_connector}
if [ -f /etc/default/xconn-agent ]; then
    set -a
    # shellcheck disable=SC1091
    . /etc/default/xconn-agent
    set +a
fi
CAPTURE=${XCONN_CAPTURE:-plughw:0,0}
PLAYBACK=${XCONN_PLAYBACK:-plughw:0,0}
if [ -x "$PREFIX/python/bin/python3" ]; then
    PYTHON="$PREFIX/python/bin/python3"
else
    PYTHON=python3
fi
export PYTHONPATH="${PYTHONPATH:-$PREFIX}"
cd "$PREFIX"
exec "$PYTHON" -m xconn_channel agent --capture "$CAPTURE" --playback "$PLAYBACK"
