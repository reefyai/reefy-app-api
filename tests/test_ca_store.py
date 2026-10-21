"""Check the actual container TLS stores, including certifi when installed."""
import os
import ssl
import unittest

BLOCKED_ROOTS = {'emSign ECC Root CA - G3', 'AAA Certificate Services'}


class PublicTrustTests(unittest.TestCase):
    def test_system_and_bundled_stores_distrust_compromised_chains(self):
        paths = [('system', os.environ.get('REEFY_TEST_CA_BUNDLE'))]
        try:
            import certifi
        except ImportError:
            pass
        else:
            paths.append(('certifi', certifi.where()))
        for name, path in paths:
            with self.subTest(store=name):
                roots = ssl.create_default_context(cafile=path).get_ca_certs()
                self.assertTrue(roots, 'empty TLS trust store')
                names = {value for cert in roots for rdn in cert['subject']
                         for key, value in rdn if key == 'commonName'}
                self.assertFalse(names & BLOCKED_ROOTS,
                                 f'compromised CA trust remains in {name}: {names & BLOCKED_ROOTS}')


if __name__ == '__main__':
    unittest.main()
