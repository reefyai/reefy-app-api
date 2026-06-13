"""Unit tests for reefy-app-api /v1/notify.

No control socket / MQTT needed: create_app takes an injectable publish.
Run: cd reefy-app-api && python -m unittest discover -s tests -v
"""
import base64
import hashlib
import hmac
import importlib
import io
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SECRET = 'test-auth-secret'


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip('=')


def make_token(caps=('notify',), iid='inst-1', slug='guppy', exp=None,
               secret=SECRET):
    header = _b64(json.dumps({'alg': 'HS256', 'typ': 'JWT'}).encode())
    claims = {'iid': iid, 'slug': slug, 'caps': list(caps)}
    if exp is not None:
        claims['exp'] = exp
    payload = _b64(json.dumps(claims).encode())
    sig = _b64(hmac.new(secret.encode(), f'{header}.{payload}'.encode(),
                        hashlib.sha256).digest())
    return f'{header}.{payload}.{sig}'


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ['AUTH_SECRET'] = SECRET
        os.environ['ARTIFACTS_DIR'] = self.tmp
        import app as appmod
        importlib.reload(appmod)
        self.appmod = appmod
        self.published = []
        self.app = appmod.create_app(
            publish=lambda suffix, payload: self.published.append((suffix, payload)))
        self.c = self.app.test_client()
        appmod._rl_last.clear()

    def _post(self, token=None, **kw):
        headers = {'Authorization': f'Bearer {token}'} if token else {}
        return self.c.post('/v1/notify', headers=headers,
                           content_type='multipart/form-data', **kw)

    def test_no_token_401(self):
        r = self._post(data={'body': 'hi'})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.published, [])

    def test_bad_signature_401(self):
        r = self._post(token=make_token(secret='wrong'), data={'body': 'hi'})
        self.assertEqual(r.status_code, 401)

    def test_expired_token_401(self):
        r = self._post(token=make_token(exp=time.time() - 10), data={'body': 'hi'})
        self.assertEqual(r.status_code, 401)

    def test_missing_capability_403(self):
        r = self._post(token=make_token(caps=['secrets:read']), data={'body': 'hi'})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.published, [])

    def test_empty_body_400(self):
        r = self._post(token=make_token(), data={})
        self.assertEqual(r.status_code, 400)

    def test_text_only_202(self):
        r = self._post(token=make_token(),
                       data={'title': 'Door', 'body': 'opened'})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(len(self.published), 1)
        suffix, meta = self.published[0]
        self.assertEqual(suffix, 'notify')
        self.assertEqual(meta['title'], 'Door')
        self.assertEqual(meta['body'], 'opened')
        self.assertEqual(meta['instance_uuid'], 'inst-1')
        self.assertEqual(meta['app_slug'], 'guppy')
        self.assertNotIn('artifact_id', meta)

    def test_attachment_202_atomic(self):
        content = b'\x00\x01FAKE-MP4-BYTES\xff'
        r = self._post(token=make_token(), data={
            'body': 'clip',
            'file': (io.BytesIO(content), 'clip.mp4', 'video/mp4'),
        })
        self.assertEqual(r.status_code, 202)
        meta = self.published[0][1]
        self.assertIn('artifact_id', meta)
        self.assertEqual(meta['content_type'], 'video/mp4')
        self.assertEqual(meta['size'], len(content))
        # File written to the artifacts dir under the id, no .tmp left.
        path = os.path.join(self.tmp, meta['artifact_id'])
        self.assertTrue(os.path.exists(path))
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), content)
        self.assertFalse(os.path.exists(path + '.tmp'))
        self.assertEqual(r.get_json()['artifact_id'], meta['artifact_id'])

    def test_rate_limited_429(self):
        tok = make_token(iid='same-inst')
        r1 = self._post(token=tok, data={'body': 'one'})
        r2 = self._post(token=tok, data={'body': 'two'})
        self.assertEqual(r1.status_code, 202)
        self.assertEqual(r2.status_code, 429)
        self.assertEqual(len(self.published), 1)

    def test_size_cap_413(self):
        # Shrink the cap, rebuild the app so MAX_CONTENT_LENGTH is tiny,
        # then send an over-cap body -> Flask 413 before the handler.
        self.appmod.MAX_ATTACH_BYTES = 50
        self.appmod._BODY_OVERHEAD = 0
        app = self.appmod.create_app(publish=lambda s, p: None)
        c = app.test_client()
        r = c.post('/v1/notify',
                   headers={'Authorization': f'Bearer {make_token()}'},
                   content_type='multipart/form-data',
                   data={'file': (io.BytesIO(b'x' * 500), 'big.bin',
                                  'application/octet-stream')})
        self.assertEqual(r.status_code, 413)

    def test_publish_failure_502(self):
        def boom(suffix, payload):
            raise RuntimeError('control socket down')
        app = self.appmod.create_app(publish=boom)
        c = app.test_client()
        r = c.post('/v1/notify',
                   headers={'Authorization': f'Bearer {make_token(iid="x")}'},
                   content_type='multipart/form-data', data={'body': 'hi'})
        self.assertEqual(r.status_code, 502)

    def test_healthz(self):
        self.assertEqual(self.c.get('/healthz').status_code, 200)

    def test_reap_age_and_size(self):
        d = self.tmp
        # Old file (mtime in the past) -> aged out.
        old = os.path.join(d, 'old')
        with open(old, 'wb') as f:
            f.write(b'x')
        past = time.time() - (self.appmod.ARTIFACT_TTL_S + 60)
        os.utime(old, (past, past))
        # Two fresh files over a tiny size cap -> oldest evicted.
        self.appmod.ARTIFACT_MAX_TOTAL_BYTES = 100
        a = os.path.join(d, 'a')
        b = os.path.join(d, 'b')
        with open(a, 'wb') as f:
            f.write(b'x' * 80)
        time.sleep(0.01)
        with open(b, 'wb') as f:
            f.write(b'x' * 80)
        self.appmod._reap(d)
        self.assertFalse(os.path.exists(old), 'aged-out file deleted')
        self.assertFalse(os.path.exists(a), 'oldest over-cap file evicted')
        self.assertTrue(os.path.exists(b), 'newest kept')


if __name__ == '__main__':
    unittest.main()
