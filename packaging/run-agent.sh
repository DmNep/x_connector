#!/bin/sh
# Запуск агента. Читает /etc/default/xconn-agent, в сеть не ходит.
set -eu
PREFIX=/opt/x_connector
if [ -f /etc/default/xconn-agent ]; then
    set -a
    # shellcheck disable=SC1091
    . /etc/default/xconn-agent
    set +a
fi
CAPTURE=${XCONN_CAPTURE:-hw:0,0}
PLAYBACK=${XCONN_PLAYBACK:-hw:0,0}
export PYTHONPATH="${PYTHONPATH:-$PREFIX}"
cd "$PREFIX"
exec python3 -m xconn_channel agent --capture "$CAPTURE" --playback "$PLAYBACK"
