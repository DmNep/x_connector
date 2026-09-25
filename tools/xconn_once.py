"""Один кадр к живому агенту: cmd / key / refresh."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xconn_channel import config
from xconn_channel.audioio import open_audio
from xconn_channel.client import Client
from xconn_channel.devcheck import check_winmm
from xconn_channel.handshake import HandshakeError
from xconn_channel.session import SessionError
from xconn_channel.transport import AudioTransport


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: xconn_once.py cmd|key|refresh [arg]", file=sys.stderr)
        return 2
    action = sys.argv[1]
    arg = " ".join(sys.argv[2:])
    check_winmm(0, 1)
    device = open_audio("winmm", in_device=0, out_device=1)
    try:
        transport = AudioTransport(device.sink, device.source, config.PROBE)
        client = Client(transport.send, transport.receive, transport=transport)
        try:
            client.connect()
        except (HandshakeError, SessionError) as err:
            print(err, file=sys.stderr)
            return 1
        if action == "cmd":
            screen = client.cmd(arg)
        elif action == "key":
            screen = client.key(arg)
        elif action == "refresh":
            screen = client.refresh()
        else:
            print("cmd|key|refresh", file=sys.stderr)
            return 2
        print(f"{screen.rows}x{screen.cols} cursor={screen.cur_row},{screen.cur_col}")
        print(screen.text())
        return 0
    except (HandshakeError, SessionError) as err:
        print(err, file=sys.stderr)
        return 1
    finally:
        device.close()


if __name__ == "__main__":
    sys.exit(main())
