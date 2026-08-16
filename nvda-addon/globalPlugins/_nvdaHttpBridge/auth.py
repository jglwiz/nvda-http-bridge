"""Loopback authentication, host validation, and secure-session state."""

from collections import defaultdict, deque
import hmac
import os
import threading
import time

from .config import TOKEN_FILE_NAME
from .errors import Forbidden, TooManyRequests, Unauthorized
from .ids import token_urlsafe


class SecurityState:
	def __init__(self, locked=False, secure_desktop=False, unknown=False):
		self._locked = bool(locked)
		self._secure_desktop = bool(secure_desktop)
		self._unknown = bool(unknown)
		self._lock = threading.RLock()

	def update(self, locked=None, secure_desktop=None, unknown=None):
		with self._lock:
			was_restricted = self._locked or self._secure_desktop or self._unknown
			if locked is not None:
				self._locked = bool(locked)
			if secure_desktop is not None:
				self._secure_desktop = bool(secure_desktop)
			if unknown is not None:
				self._unknown = bool(unknown)
			is_restricted = self._locked or self._secure_desktop or self._unknown
			return was_restricted, is_restricted

	def restricted(self):
		with self._lock:
			return self._locked or self._secure_desktop or self._unknown

	def snapshot(self):
		with self._lock:
			return {
				"locked": self._locked,
				"secureDesktop": self._secure_desktop,
				"unknown": self._unknown,
				"restricted": self._locked or self._secure_desktop or self._unknown,
			}


class TokenManager:
	def __init__(self, config_path, token=None):
		self.path = os.path.join(config_path, TOKEN_FILE_NAME)
		self.token = token or self._load_or_create()

	def _load_or_create(self):
		token = None
		try:
			with open(self.path, "r", encoding="ascii") as token_file:
				candidate = token_file.read().strip()
			if len(candidate) >= 32:
				token = candidate
		except OSError:
			pass
		if token is None:
			token = token_urlsafe(32)
		self._publish(self.path, token)
		return token

	@staticmethod
	def _publish(path, token):
		temp_path = "%s.%s.tmp" % (path, os.getpid())
		try:
			os.makedirs(os.path.dirname(path), exist_ok=True)
			with open(temp_path, "x", encoding="ascii") as token_file:
				token_file.write(token)
			os.chmod(temp_path, 0o600)
			os.replace(temp_path, path)
		except OSError:
			try:
				os.remove(temp_path)
			except OSError:
				pass

	def authorize(self, supplied):
		if not supplied or not hmac.compare_digest(self.token, supplied):
			raise Unauthorized()


def extract_token(headers):
	authorization = headers.get("Authorization", "")
	if authorization.lower().startswith("bearer "):
		return authorization[7:].strip()
	return headers.get("X-NVDA-HTTP-Token", "").strip()


def validate_host(host_header, port):
	if not host_header:
		raise Forbidden("A loopback Host header is required")
	host = host_header.strip().lower()
	allowed = {
		"127.0.0.1",
		"localhost",
		"127.0.0.1:%s" % port,
		"localhost:%s" % port,
	}
	if host not in allowed:
		raise Forbidden("Only loopback Host headers are accepted")


def validate_browser_context(headers, port):
	"""Reject cross-site browser requests before they can trigger tree work."""
	origin = headers.get("Origin", "").strip().lower().rstrip("/")
	if origin:
		allowed_origins = {
			"http://127.0.0.1:%s" % port,
			"http://localhost:%s" % port,
		}
		if origin not in allowed_origins:
			raise Forbidden("Cross-site browser requests are not accepted")
	fetch_site = headers.get("Sec-Fetch-Site", "").strip().lower()
	if fetch_site and fetch_site not in ("none", "same-origin"):
		raise Forbidden("Cross-site browser requests are not accepted")


class RateLimiter:
	def __init__(self, limit=30, window_seconds=10.0, monotonic=None):
		self._limit = limit
		self._window = window_seconds
		self._monotonic = monotonic or time.monotonic
		self._entries = defaultdict(deque)
		self._lock = threading.Lock()

	def check(self, key):
		now = self._monotonic()
		with self._lock:
			entries = self._entries[key]
			while entries and entries[0] <= now - self._window:
				entries.popleft()
			if len(entries) >= self._limit:
				raise TooManyRequests("The action rate limit was exceeded")
			entries.append(now)
