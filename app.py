"""reefy-app-api: the device-side platform sidecar.

Apps on the device reach Reefy platform capabilities through this single
local HTTP surface (injected as REEFY_API_URL + REEFY_API_TOKEN, the
llm-proxy pattern). v1 capability: `notify` - send the owner a message
with an optional video/binary attachment, delivered over Telegram.

The app never sees MQTT, the cloud, the bot token, or the channel: this
container attributes the call (from the token), stores any attachment on
a shared volume the device's reverse proxy serves over the tunnel, and
forwards metadata to the control plane over a Varlink socket - holding
no device certs of its own.
"""
import glob
import os
import secrets
import threading
import time

from flask import Flask, jsonify, request

from jwt_min import verify_jwt
import varlink_pub

# 50 MB Telegram bot send limit for video/document. Cap the whole
# request body a touch higher to leave room for the small text fields.
MAX_ATTACH_BYTES = 50 * 1024 * 1024
_BODY_OVERHEAD = 1 * 1024 * 1024

# Artifact retention. A file only needs to live long enough for whoever
# consumes it (the cloud mints a short-lived capability URL; the consumer
# - e.g. Telegram's servers - fetches it once) to grab it within a few
# minutes. Cleanup is EVENT-DRIVEN (reap on each attachment write + once
# at startup), not a polling loop, since attachments are rare: cleanup
# runs exactly when the disk grows. Reap by age, with a total-size safety
# cap (oldest first) so a burst can't fill the shared volume / device
# disk. Both env-overridable.
ARTIFACT_TTL_S = int(os.environ.get('REEFY_ARTIFACT_TTL_S', str(15 * 60)))
ARTIFACT_MAX_TOTAL_BYTES = int(
    os.environ.get('REEFY_ARTIFACT_MAX_TOTAL_BYTES', str(1024 * 1024 * 1024)))


def _reap(artifacts_dir: str) -> None:
    """Delete artifacts (and stale .tmp) past the age TTL, then enforce
    the total-size cap by deleting oldest-first. Best-effort; never
    touches files newer than the TTL, so the just-written one is safe."""
    now = time.time()
    entries = []
    for path in glob.glob(os.path.join(artifacts_dir, '*')):
        try:
            st = os.stat(path)
        except OSError:
            continue
        if now - st.st_mtime > ARTIFACT_TTL_S:
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        entries.append((st.st_mtime, st.st_size, path))
    total = sum(e[1] for e in entries)
    for _mtime, size, path in sorted(entries):  # oldest first
        if total <= ARTIFACT_MAX_TOTAL_BYTES:
            break
        try:
            os.unlink(path)
            total -= size
        except OSError:
            pass

# Local coarse rate limit - NOT a security control. A compromised device
# controls this container AND holds the device MQTT cert, so it can
# publish `notify` directly and bypass anything here. The authoritative
# per-account limit (anti-flood / anti-abuse against the Reefy service,
# the Telegram quota, and the owner's chat) is enforced CLOUD-SIDE in the
# backend notify handler, where an adversary can't tamper with it. This
# only stops a buggy-but-honest app from needless MQTT churn.
LOCAL_MIN_INTERVAL_S = 1.0

_rl_lock = threading.Lock()
_rl_last: dict[str, float] = {}


def _rate_ok(instance_uuid: str) -> bool:
    now = time.monotonic()
    with _rl_lock:
        last = _rl_last.get(instance_uuid, 0.0)
        if now - last < LOCAL_MIN_INTERVAL_S:
            return False
        _rl_last[instance_uuid] = now
    return True


def create_app(publish=varlink_pub.publish) -> Flask:
    """`publish` is injectable so tests can run without the control socket."""
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = MAX_ATTACH_BYTES + _BODY_OVERHEAD
    auth_secret = os.environ.get('AUTH_SECRET', '')
    artifacts_dir = os.environ.get('ARTIFACTS_DIR', '/var/reefy/artifacts')
    os.makedirs(artifacts_dir, exist_ok=True)
    _reap(artifacts_dir)  # clear anything left by a prior run

    def _claims():
        """Validate the bearer token; return claims dict or None."""
        hdr = request.headers.get('Authorization', '')
        if not hdr.startswith('Bearer '):
            return None
        return verify_jwt(hdr[7:], auth_secret)

    @app.get('/healthz')
    def healthz():
        return jsonify(ok=True)

    @app.post('/v1/notify')
    def notify():
        claims = _claims()
        if not claims:
            return jsonify(error='invalid or missing token'), 401
        if 'notify' not in (claims.get('caps') or []):
            return jsonify(error='app lacks the notify capability'), 403
        instance_uuid = claims.get('iid', '')
        app_slug = claims.get('slug', '')

        if not _rate_ok(instance_uuid):
            return jsonify(error='rate limited'), 429

        title = (request.form.get('title') or '').strip()
        body = (request.form.get('body') or '').strip()
        if not title and not body:
            return jsonify(error='title or body required'), 400

        meta = {
            'instance_uuid': instance_uuid,
            'app_slug': app_slug,
            'title': title,
            'body': body,
            'ts': time.time(),
        }

        # Optional attachment: stream to <id>.tmp, fsync, atomic rename
        # so reefy-proxy (ro-mounted on the same volume) only ever opens
        # a COMPLETE file. (Flask's MAX_CONTENT_LENGTH already 413s an
        # over-cap body before we get here.)
        f = request.files.get('file')
        if f is not None and f.filename:
            artifact_id = secrets.token_hex(16)
            tmp = os.path.join(artifacts_dir, artifact_id + '.tmp')
            final = os.path.join(artifacts_dir, artifact_id)
            try:
                f.save(tmp)
                with open(tmp, 'rb') as fh:
                    os.fsync(fh.fileno())
                size = os.path.getsize(tmp)
                if size > MAX_ATTACH_BYTES:
                    os.unlink(tmp)
                    return jsonify(error='attachment exceeds 50MB'), 413
                os.replace(tmp, final)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            meta['artifact_id'] = artifact_id
            meta['content_type'] = f.mimetype or 'application/octet-stream'
            meta['size'] = size
            # Immediate reap enforces the size cap right when disk grows.
            _reap(artifacts_dir)
            # And a one-shot timer cleans THIS file at its TTL even if no
            # further notify ever arrives (otherwise the last artifact
            # would linger until the next write/restart). Fires once then
            # exits - no idle polling.
            t = threading.Timer(ARTIFACT_TTL_S + 5, _reap, args=(artifacts_dir,))
            t.daemon = True
            t.start()

        try:
            publish('notify', meta)
        except Exception as e:
            # The attachment (if any) is already written; a transient
            # MQTT failure means the notify did not go out. Report it so
            # the app can retry; the orphan file is reaped by retention.
            return jsonify(error=f'publish failed: {e}'), 502

        resp = {'ok': True}
        if 'artifact_id' in meta:
            resp['artifact_id'] = meta['artifact_id']
        return jsonify(resp), 202

    return app


# gunicorn entrypoint: `gunicorn app:app`
app = create_app()
