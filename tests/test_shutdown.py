"""stop() must not leave a traceback behind.

Every service stop used to log a bare "Exception in thread Thread-1": stop()
closed the ZMQ REP socket and terminated the context while _command_loop was
still inside poller.poll() on it, which raised in that thread, and the process
exited before the traceback finished printing. Reproduced under --sim on about
two stops in three.

This runs the real command thread on real ZMQ sockets and fails if any thread
raises while stop() tears them down.
"""

import contextlib
import io
import sys
import threading
import types

import pytest


def _bare_server(phaser_headless, zmq):
    """Just what run()'s command thread and stop() touch."""
    h = phaser_headless.PhaserHeadless.__new__(phaser_headless.PhaserHeadless)
    h.running = True
    h.sweeping = False
    h.mode = "idle"
    h.cal_process = None
    h.ctx = zmq.Context()
    h.pub_socket = h.ctx.socket(zmq.PUB)
    h.pub_socket.bind_to_random_port("tcp://127.0.0.1")
    h.rep_socket = h.ctx.socket(zmq.REP)
    h.rep_socket.bind_to_random_port("tcp://127.0.0.1")
    return h


@pytest.mark.parametrize("attempt", range(5))
def test_stop_leaves_the_command_thread_quietly(attempt):
    zmq = pytest.importorskip("zmq")
    sys.modules.setdefault("adi", types.ModuleType("adi"))
    try:
        import phaser_headless
    except Exception as exc:  # noqa: BLE001 - msgpack etc. absent
        pytest.skip(f"phaser_headless unavailable: {exc}")

    raised = []
    previous_hook = threading.excepthook
    threading.excepthook = lambda args: raised.append(args.exc_value)
    try:
        h = _bare_server(phaser_headless, zmq)
        h._cmd_thread = threading.Thread(target=h._command_loop, daemon=True)
        h._cmd_thread.start()
        # Let it get into poll(), where the old stop() caught it.
        threading.Event().wait(0.05 + 0.02 * attempt)
        with contextlib.redirect_stdout(io.StringIO()):
            h.stop()
        h._cmd_thread.join(timeout=2.0)
    finally:
        threading.excepthook = previous_hook

    assert not h._cmd_thread.is_alive(), "command thread outlived stop()"
    assert raised == [], f"a thread raised during stop(): {raised!r}"
