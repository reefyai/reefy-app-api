"""Minimal Varlink client: calls io.reefy.Control.PublishEvent on the
reefy-control sidecar socket so an app's notify metadata reaches the
cloud through control's single persistent MQTT connection.

Deliberately tiny and dependency-free: this container holds NO device
certs and NO MQTT library - control owns the connection and the device
mTLS key. Varlink wire format is one JSON object terminated by a NUL
byte, in each direction.
"""
import json
import os
import socket

CONTROL_SOCK = os.environ.get(
    'REEFY_CONTROL_SOCK', '/run/reefy-sidecar/control.sock')


def publish(suffix: str, payload: dict, timeout: float = 15.0) -> None:
    """Publish one event via reefy-control. Raises on any failure so the
    caller can surface it (and the app can retry)."""
    req = json.dumps({
        'method': 'io.reefy.Control.PublishEvent',
        'parameters': {'suffix': suffix, 'payload': json.dumps(payload)},
    }).encode() + b'\x00'
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(CONTROL_SOCK)
            s.sendall(req)
            buf = bytearray()
            while not buf.endswith(b'\x00'):
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
    except OSError as e:
        raise RuntimeError(f'control socket: {e}') from e
    resp = json.loads(bytes(buf).rstrip(b'\x00') or b'{}')
    if resp.get('error'):
        raise RuntimeError(f"varlink error {resp['error']}: "
                           f"{resp.get('parameters')}")
    params = resp.get('parameters', {})
    if not params.get('ok', False):
        raise RuntimeError(params.get('error') or 'publish failed')
