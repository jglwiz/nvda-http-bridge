import os
import tempfile
import unittest

from support import GLOBAL_PLUGINS  # noqa: F401

from _nvdaHttpBridge.auth import TokenManager
from _nvdaHttpBridge.config import TOKEN_FILE_NAME


class TokenManagerTests(unittest.TestCase):
	def test_token_is_published_under_the_canonical_name(self):
		with tempfile.TemporaryDirectory() as config_path:
			manager = TokenManager(config_path)

			with open(os.path.join(config_path, TOKEN_FILE_NAME), "r", encoding="ascii") as token_file:
				self.assertEqual(manager.token, token_file.read())


if __name__ == "__main__":
	unittest.main()
