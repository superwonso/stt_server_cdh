from __future__ import annotations

import errno
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock


class ServerStartPortTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Run only the production script's socket probe. Never launch the
        # server, read private configuration, or touch its managed port/PID.
        script = (
            Path(__file__).resolve().parents[1] / "scripts" / "start-server.sh"
        ).read_text(encoding="utf-8")
        function = script.split("port_is_free() {\n", 1)[1].split("\n}\n", 1)[0]
        body = function.split("<<'PY'", 1)[1].split("\n", 1)[1].rsplit("\nPY", 1)[0]
        cls.probe_code = compile(body, "start-server.sh:port_is_free", "exec")

    def probe(self, port: int) -> None:
        with mock.patch.object(sys, "argv", ["-", str(port)]):
            exec(self.probe_code, {"__name__": "__main__"})

    def test_probe_accepts_an_unused_ephemeral_loopback_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]

        self.probe(port)

    def test_probe_rejects_an_active_listener_even_with_reuseaddr(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)

            with self.assertRaises(OSError) as rejected:
                self.probe(listener.getsockname()[1])

        self.assertEqual(rejected.exception.errno, errno.EADDRINUSE)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux/WSL TCP restart regression")
    def test_closed_server_connection_does_not_block_immediate_restart(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.settimeout(2)
            listener.bind(("127.0.0.1", 0))
            address = listener.getsockname()
            listener.listen(1)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(address)
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(2)
                    # Make the server the active closer. Once the peer closes,
                    # this server port retains the old connection in TIME_WAIT.
                    connection.shutdown(socket.SHUT_WR)
                    self.assertEqual(client.recv(1), b"")

        # The old probe would report a busy port despite no listener remaining.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as old_probe:
            with self.assertRaises(OSError) as rejected:
                old_probe.bind(address)
        self.assertEqual(rejected.exception.errno, errno.EADDRINUSE)

        self.probe(address[1])


if __name__ == "__main__":
    unittest.main()
