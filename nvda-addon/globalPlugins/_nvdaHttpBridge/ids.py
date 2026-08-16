"""Cryptographically strong URL-safe IDs without the optional secrets module.

NVDA 2025.3.3 ships a deliberately reduced Python standard library that does
not include ``secrets``. ``os.urandom`` remains available and provides the same
operating-system CSPRNG needed for opaque identifiers.
"""

import base64
import os


def random_urlsafe(nbytes):
	return base64.urlsafe_b64encode(os.urandom(nbytes)).rstrip(b"=").decode("ascii")
