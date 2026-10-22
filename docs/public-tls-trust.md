# Public TLS trust remediation

The image has its own Alpine trust store. Firmware CA updates do not update
running containers. Install the fixed Alpine ca-certificates package
(20261020-r0 or newer) and check the actual Python default SSL context during
the image build. If certifi is present, check its independent bundle too.
The build fails if either store is empty or trusts emSign ECC Root CA - G3
or AAA Certificate Services. These anchors cover the native and Sectigo
cross-signed compromised eMudhra chains.

Sources:
- https://emudhra.we-have-a-problem.com/
- https://pkgs.alpinelinux.org/packages?name=ca-certificates

The existing sha-<7chars> image format is unchanged. Feature branches publish
immutable images for validation; the floating main tag is updated only by
main. Pull a current base image before building so its package repository
metadata is current.

## Validation

The same committed test fails with the unmodified firmware's public CA
package (Debian 20260223) and passes with Debian 20260816+nmu1. These are
portable trust-policy controls, not substitutes for the Alpine image build.
The Docker build runs the test against the final Alpine store and installed
Python dependencies. Run the existing notification tests separately:

```sh
python -m unittest discover -s tests -p test_notify.py
python tests/test_ca_store.py
```

A default-context check requires a host with a corrected system store; to
check a specific public bundle, set REEFY_TEST_CA_BUNDLE to its path. The
certifi check, when available, always uses certifi's actual installed bundle.
