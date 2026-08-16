"""Bounded loopback HTTP transport for the bridge service."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import socket
import threading
import time
from urllib.parse import parse_qs, urlparse

from .auth import extract_token, validate_browser_context, validate_host
from .config import (
	HOST,
	MAX_ACTIVE_REQUESTS,
	MAX_CONNECTIONS,
	MAX_HEADER_BYTES,
	MAX_REQUEST_BODY,
	MAX_REQUEST_LINE_BYTES,
	MAX_SSE_CLIENTS,
	PLUGIN_VERSION,
	PORT,
	SERVER_JOIN_TIMEOUT_SECONDS,
	SOCKET_TIMEOUT_SECONDS,
)
from .errors import BadRequest, BridgeError, Conflict, ServiceUnavailable, TooManyRequests


_EXPORT_ROUTE = re.compile(r"^/v1/tree/exports/([A-Za-z0-9_-]+)(/data)?$")
_BACKUP_ROUTE = re.compile(r"^/v1/backups/([A-Za-z0-9_-]+)$")
_ACTION_ROUTE = re.compile(r"^/v1/actions/([a-z-]+)$")
_OBJECT_ID_ROUTE = re.compile(r"^/v1/objects/by-id/([A-Za-z0-9_.-]+)$")
_SPEECH_DICTIONARY_ROUTE = re.compile(r"^/v1/speech-dictionaries/(default|voice|temp)(/validate)?$")
_SYMBOL_DICTIONARY_ROUTE = re.compile(r"^/v1/symbol-dictionaries/([A-Za-z0-9_-]+)$")


class BoundedHTTPServer(ThreadingHTTPServer):
	allow_reuse_address = True
	daemon_threads = True
	block_on_close = False

	def __init__(self, service, logger=None, address=(HOST, PORT)):
		self.service = service
		self.logger = logger
		self.closing = threading.Event()
		self._connection_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
		self.request_slots = threading.BoundedSemaphore(MAX_ACTIVE_REQUESTS)
		self.sse_slots = threading.BoundedSemaphore(MAX_SSE_CLIENTS)
		self._thread = None
		self._active_condition = threading.Condition()
		self._active_requests = 0
		self._request_sockets = set()
		self._stop_lock = threading.Lock()
		self._stopped = False
		super().__init__(address, RequestHandler)

	def process_request(self, request, client_address):
		if not self._connection_slots.acquire(blocking=False):
			body = b'{"error":{"code":"serviceUnavailable","message":"Connection limit reached"}}'
			try:
				headers = (
					"HTTP/1.1 503 Service Unavailable\r\n"
					"Content-Type: application/json\r\n"
					"Cache-Control: no-store\r\n"
					"Connection: close\r\n"
					"Content-Length: %s\r\n\r\n" % len(body)
				).encode("ascii")
				request.sendall(headers + body)
			except OSError:
				pass
			self.shutdown_request(request)
			return
		try:
			with self._active_condition:
				self._active_requests += 1
				self._request_sockets.add(request)
			super().process_request(request, client_address)
		except Exception:
			with self._active_condition:
				self._request_sockets.discard(request)
				self._active_requests -= 1
				self._active_condition.notify_all()
			self._connection_slots.release()
			raise

	def process_request_thread(self, request, client_address):
		try:
			super().process_request_thread(request, client_address)
		finally:
			with self._active_condition:
				self._request_sockets.discard(request)
				self._active_requests -= 1
				self._active_condition.notify_all()
			self._connection_slots.release()

	def start(self):
		if self._thread and self._thread.is_alive():
			return
		self._thread = threading.Thread(target=self.serve_forever, name="nvdaHttpBridgeServer", daemon=True)
		self._thread.start()

	def begin_close(self):
		self.closing.set()
		with self._active_condition:
			requests = list(self._request_sockets)
		for request in requests:
			try:
				request.shutdown(socket.SHUT_RDWR)
			except OSError:
				pass

	def stop(self):
		with self._stop_lock:
			if self._stopped:
				return
			self.begin_close()
			if self._thread and self._thread.is_alive():
				self.shutdown()
			deadline = time.monotonic() + SERVER_JOIN_TIMEOUT_SECONDS
			with self._active_condition:
				while self._active_requests:
					remaining = deadline - time.monotonic()
					if remaining <= 0:
						break
					self._active_condition.wait(remaining)
			self.server_close()
			if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
				self._thread.join(max(0.0, deadline - time.monotonic()))
			self._stopped = True


class RequestHandler(BaseHTTPRequestHandler):
	protocol_version = "HTTP/1.1"
	server_version = "NVDAHTTPBridge/%s" % PLUGIN_VERSION
	sys_version = ""

	def setup(self):
		super().setup()
		self.connection.settimeout(SOCKET_TIMEOUT_SECONDS)

	def handle(self):
		try:
			super().handle()
		except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
			# Server shutdown deliberately interrupts keep-alive sockets.
			return

	def log_message(self, format, *args):
		if self.server.logger:
			self.server.logger.debug("nvdaHttpBridge: %s", format % args)

	def do_GET(self):
		self._dispatch(self._get)

	def do_POST(self):
		self._dispatch(self._post)

	def do_DELETE(self):
		self._dispatch(self._delete)

	def do_PATCH(self):
		self._dispatch(self._patch)

	def do_PUT(self):
		self._dispatch(self._put)

	def do_OPTIONS(self):
		self._dispatch(
			lambda: self._json_response(
				405,
				{"error": {"code": "methodNotAllowed", "message": "CORS is not enabled"}},
			),
		)

	def _dispatch(self, callback):
		try:
			self._validate_request_metadata()
			validate_host(self.headers.get("Host"), self.server.server_address[1])
			validate_browser_context(self.headers, self.server.server_address[1])
			callback()
		except BridgeError as error:
			self._json_response(error.status, error.as_dict())
		except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, socket.timeout):
			return
		except Exception:
			if self.server.logger:
				self.server.logger.exception("nvdaHttpBridge: unhandled HTTP request error")
			try:
				self._json_response(
					500,
					{"error": {"code": "internalError", "message": "The request could not be completed"}},
				)
			except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
				return

	def _validate_request_metadata(self):
		if len(self.raw_requestline) > MAX_REQUEST_LINE_BYTES:
			raise BadRequest("The request line exceeds the configured limit")
		header_bytes = 2
		for name, value in self.headers.items():
			header_bytes += len(name.encode("utf-8", errors="replace"))
			header_bytes += len(value.encode("utf-8", errors="replace")) + 4
		if header_bytes > MAX_HEADER_BYTES:
			raise BadRequest("The request headers exceed the configured limit")

	def _get(self):
		parsed = urlparse(self.path)
		path = parsed.path.rstrip("/") or "/"
		params = parse_qs(parsed.query, keep_blank_values=True)
		service = self.server.service

		if path == "/health":
			self._json_response(200, service.health())
			return
		if path == "/v1/version":
			self._json_response(200, service.version())
			return
		if path == "/v1/capabilities":
			self._json_response(200, service.capabilities())
			return
		if path == "/v1/events":
			self._auth(sensitive=True)
			self._sse(params)
			return

		if not self.server.request_slots.acquire(blocking=False):
			raise TooManyRequests("The active request limit was reached")
		try:
			if path == "/v1/settings/categories":
				self._auth(sensitive=True)
				self._json_response(200, service.settings_categories())
			elif path == "/v1/settings/general":
				self._auth(sensitive=True)
				self._json_response(200, service.general_settings())
			elif path == "/v1/speech-dictionaries":
				self._auth(sensitive=True)
				self._json_response(200, service.speech_dictionary_list())
			elif (match := _SPEECH_DICTIONARY_ROUTE.match(path)) and not match.group(2):
				self._auth(sensitive=True)
				self._json_response(200, service.speech_dictionary(match.group(1)))
			elif (match := _SYMBOL_DICTIONARY_ROUTE.match(path)):
				self._auth(sensitive=True)
				self._json_response(200, service.symbol_dictionary(match.group(1)))
			elif path == "/v1/gestures":
				self._auth(sensitive=True)
				unknown = sorted(set(params) - {"context", "filter"})
				if unknown:
					raise BadRequest("Unknown gesture query parameters")
				self._json_response(200, service.gesture_mappings(
					self._one(params, "context", "current"),
					self._one(params, "filter"),
				))
			elif _OBJECT_ID_ROUTE.match(path):
				self._auth()
				object_id = _OBJECT_ID_ROUTE.match(path).group(1)
				self._json_response(200, service.object_by_id(object_id, params))
			elif path.startswith("/v1/objects/"):
				self._auth()
				root = path.rsplit("/", 1)[-1]
				self._json_response(200, service.object_snapshot(root, params))
			elif path == "/v1/tree":
				self._auth()
				self._json_response(200, service.tree(params))
			elif path == "/v1/speech":
				self._auth(sensitive=True)
				self._json_response(200, service.speech(self._one(params, "last")))
			elif path == "/v1/log":
				self._auth(sensitive=True)
				self._json_response(200, service.log_tail(self._one(params, "last", 50)))
			else:
				match = _EXPORT_ROUTE.match(path)
				if match:
					self._auth(sensitive=True)
					job_id, data_suffix = match.groups()
					if data_suffix:
						data_file, length = service.open_export_data(job_id)
						self._file_response(
							data_file,
							length,
							job_id + ".ndjson",
							lambda: service.export_download_allowed(job_id),
							"application/x-ndjson; charset=utf-8",
						)
					else:
						self._json_response(200, service.export_status(job_id))
				else:
					match = _BACKUP_ROUTE.match(path)
					if match:
						self._auth(sensitive=True)
						self._json_response(200, service.backup_status(match.group(1)))
					else:
						raise NotFoundError()
		finally:
			self.server.request_slots.release()

	def _post(self):
		path = urlparse(self.path).path.rstrip("/")
		service = self.server.service
		if not self.server.request_slots.acquire(blocking=False):
			raise TooManyRequests("The active request limit was reached")
		try:
			match = _SPEECH_DICTIONARY_ROUTE.match(path)
			if match and match.group(2) == "/validate":
				self._auth(write=True)
				self._json_response(200, service.validate_speech_dictionary(match.group(1), self._read_json()))
				return
			if path == "/v1/tree/exports":
				self._auth(write=True, sensitive=True)
				self._json_response(202, service.create_export(self._read_json()))
				return
			if path == "/v1/backups":
				self._auth(write=True, sensitive=True)
				self._json_response(202, service.create_backup(self._read_json()))
				return
			match = _ACTION_ROUTE.match(path)
			if match:
				self._auth(write=True)
				self._json_response(200, service.action(match.group(1), self._read_json()))
				return
			raise NotFoundError()
		finally:
			self.server.request_slots.release()

	def _patch(self):
		path = urlparse(self.path).path.rstrip("/")
		if not self.server.request_slots.acquire(blocking=False):
			raise TooManyRequests("The active request limit was reached")
		try:
			self._auth(write=True)
			if path == "/v1/settings/general":
				self._json_response(200, self.server.service.patch_general_settings(self._read_json()))
				return
			if path == "/v1/gestures":
				self._json_response(200, self.server.service.patch_gestures(self._read_json()))
				return
			raise NotFoundError()
		finally:
			self.server.request_slots.release()

	def _put(self):
		path = urlparse(self.path).path.rstrip("/")
		if not self.server.request_slots.acquire(blocking=False):
			raise TooManyRequests("The active request limit was reached")
		try:
			self._auth(write=True)
			match = _SPEECH_DICTIONARY_ROUTE.match(path)
			if match and not match.group(2):
				self._json_response(200, self.server.service.put_speech_dictionary(match.group(1), self._read_json()))
				return
			match = _SYMBOL_DICTIONARY_ROUTE.match(path)
			if match:
				self._json_response(200, self.server.service.put_symbol_dictionary(match.group(1), self._read_json()))
				return
			raise NotFoundError()
		finally:
			self.server.request_slots.release()

	def _delete(self):
		path = urlparse(self.path).path.rstrip("/")
		match = _EXPORT_ROUTE.match(path)
		if match and not match.group(2):
			self._auth(write=True, sensitive=True)
			self._json_response(202, self.server.service.cancel_export(match.group(1)))
			return
		match = _BACKUP_ROUTE.match(path)
		if match:
			self._auth(write=True, sensitive=True)
			self._json_response(202, self.server.service.cancel_backup(match.group(1)))
			return
		raise NotFoundError()

	def _auth(self, write=False, sensitive=False):
		self.server.service.authorize(extract_token(self.headers), write=write, sensitive=sensitive)

	def _read_json(self):
		if self.headers.get("Transfer-Encoding"):
			raise BadRequest("Transfer-Encoding is not supported")
		raw_length = self.headers.get("Content-Length", "0")
		try:
			length = int(raw_length)
		except ValueError:
			raise BadRequest("Content-Length must be an integer")
		if length < 0 or length > MAX_REQUEST_BODY:
			raise BadRequest("The request body exceeds the configured limit")
		body = self.rfile.read(length) if length else b"{}"
		try:
			return json.loads(body.decode("utf-8"))
		except (UnicodeDecodeError, json.JSONDecodeError):
			raise BadRequest("The request body must be valid UTF-8 JSON")

	def _sse(self, params):
		if not self.server.sse_slots.acquire(blocking=False):
			raise TooManyRequests("The SSE client limit was reached")
		try:
			types_value = self._one(params, "types", "")
			event_types = tuple(item for item in str(types_value).split(",") if item)
			last_header = self.headers.get("Last-Event-ID", "")
			last_id = 0
			instance_mismatch = False
			if last_header:
				try:
					instance_id, raw_id = last_header.rsplit(":", 1)
					last_id = int(raw_id)
					instance_mismatch = instance_id != self.server.service.events.instance_id
				except (ValueError, TypeError):
					instance_mismatch = True
			self.send_response(200)
			self.send_header("Content-Type", "text/event-stream; charset=utf-8")
			self.send_header("Cache-Control", "no-store")
			self.send_header("Connection", "keep-alive")
			self.end_headers()
			if instance_mismatch:
				last_id = self.server.service.events.recovery_cursor(0)
				if not self._write_sse("reset", last_id, {"reason": "instanceChanged"}):
					return
			while not self.server.closing.is_set():
				items, gap, closed = self.server.service.events.wait_after(last_id, event_types, 15.0, 100)
				if closed or self.server.service.security_state.restricted():
					break
				if gap:
					last_id = self.server.service.events.recovery_cursor(last_id)
					if not self._write_sse("reset", last_id, {"reason": "bufferOverflow"}):
						break
				for item in items:
					# A security transition can clear/reset the buffer after wait_after
					# returns. Validate the captured epoch immediately before every write
					# and never relabel an old item with the new instance ID.
					if not self._write_sse(
						item["type"],
						item["id"],
						item,
						instance_id=item["instanceId"],
					):
						return
					last_id = item["id"]
				if not items:
					if (
						self.server.service.security_state.restricted()
						or not self.server.service.events.is_current()
					):
						break
					self.wfile.write(b": keepalive\n\n")
					self.wfile.flush()
		finally:
			self.server.sse_slots.release()

	def _write_sse(self, event_name, event_id, data, instance_id=None):
		events = self.server.service.events
		instance_id = instance_id or events.instance_id
		if self.server.service.security_state.restricted() or not events.is_current(instance_id):
			return False
		payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
		message = "id: %s:%s\nevent: %s\ndata: %s\n\n" % (instance_id, event_id, event_name, payload)
		self.wfile.write(message.encode("utf-8"))
		self.wfile.flush()
		return True

	def _json_response(self, status, data):
		body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
		self.send_response(status)
		self.send_header("Content-Type", "application/json; charset=utf-8")
		self.send_header("Content-Length", str(len(body)))
		self.send_header("Cache-Control", "no-store")
		self.send_header("X-Content-Type-Options", "nosniff")
		self.end_headers()
		self.wfile.write(body)

	def _file_response(self, data_file, length, download_name, availability_check, content_type):
		try:
			if (
				self.server.closing.is_set()
				or not availability_check()
			):
				raise Conflict("The download data is no longer available")
			self.send_response(200)
			self.send_header("Content-Type", content_type)
			self.send_header("Content-Length", str(length))
			self.send_header("Content-Disposition", 'attachment; filename="%s"' % download_name)
			self.send_header("Cache-Control", "no-store")
			self.send_header("X-Content-Type-Options", "nosniff")
			self.end_headers()
			while True:
				if (
					self.server.closing.is_set()
					or not availability_check()
				):
					self.close_connection = True
					return
				chunk = data_file.read(16 * 1024)
				if not chunk:
					break
				# Re-check after disk I/O and immediately before exposing the chunk.
				if (
					self.server.closing.is_set()
					or not availability_check()
				):
					self.close_connection = True
					return
				self.wfile.write(chunk)
		finally:
			data_file.close()

	@staticmethod
	def _one(params, name, default=None):
		values = params.get(name)
		if values is None:
			return default
		if len(values) != 1:
			raise BadRequest("Parameter '%s' must be supplied once" % name)
		return values[0]


class NotFoundError(BridgeError):
	status = 404
	code = "notFound"
	default_message = "The requested endpoint was not found"
