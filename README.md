# reefy-app-api

The device-side **platform sidecar** for Reefy: a single local HTTP API
through which apps on a device reach Reefy platform capabilities, without
knowing anything about MQTT, the cloud, channels, or secrets.

Runs as an infra container on the device (port **9081**) on the local
docker network. The Reefy platform injects discovery + auth into every
app container.

## How apps use it

Each app container receives:

```
REEFY_API_URL   = http://reefy-app-api:9081
REEFY_API_TOKEN = <per-instance JWT, scoped to the app's capabilities>
```

An app sends a notification (optionally with a video/binary attachment)
in one multipart call - like attaching a file to an email:

```bash
curl -s "$REEFY_API_URL/v1/notify" \
  -H "Authorization: Bearer $REEFY_API_TOKEN" \
  -F title="Door forced" \
  -F body="Person jumped over the fence at CAM1" \
  -F file=@/clip.mp4
```

The owner receives it on their phone, with the text as the caption on
the video. No API keys, no channel knowledge, no setup.

## v1 capability: `notify`

`POST /v1/notify` (multipart)
- fields: `title`, `body` (at least one required)
- `file`: optional attachment, <= 50 MB
- auth: `Authorization: Bearer <REEFY_API_TOKEN>`; the app must hold the
  `notify` capability (declared in its app manifest, granted at install)

Returns `202 {"ok": true, "artifact_id": "..."}`.

`GET /healthz` -> `200 {"ok": true}`.

## Architecture (why it holds no secrets)

```
app --HTTP(multipart)--> reefy-app-api --Varlink--> reefy-control --MQTT--> cloud
                              |                       (persistent client,
                              | writes attachment      owns the device key)
                              v
                       reefy-artifacts volume <--ro-- reefy-proxy --tunnel--> phone fetches the file
```

- **No MQTT, no device certs in this container.** Notify metadata is
  forwarded to the device control plane over a Varlink unix socket
  (`io.reefy.Control.PublishEvent`, suffix allowlisted to `notify`),
  which publishes over its single persistent MQTT connection. The device
  identity key never enters this container.
- **Attachments** are written atomically (`<id>.tmp` -> fsync -> rename)
  to a shared named volume that the device's reverse proxy serves to the
  phone over the tunnel via a short-lived capability URL. Attachment
  bytes go device -> phone directly; only metadata crosses the cloud.
- **Per-app token**: an HS256 JWT signed with the per-device secret,
  carrying `{iid, slug, caps}`; validated locally (`jwt_min.py`).
- **Rate limit**: the coarse local limit here is courtesy only - the
  authoritative per-account limit is enforced server-side (a compromised
  device bypasses anything local).

## Test

```bash
python -m venv .venv-test && .venv-test/bin/pip install flask
.venv-test/bin/python -m unittest discover -s tests -v
```

## Build

GitHub Actions builds + pushes `ghcr.io/reefyai/reefy-app-api:sha-<short>`
and `:main` on push to `main`.
