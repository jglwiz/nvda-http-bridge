#!/usr/bin/env python3
"""Safe stdlib client for the local NVDA HTTP Bridge."""

from __future__ import annotations

import argparse
import ctypes
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import queue
import re
import shutil
import socket
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


DEFAULT_BASE_URL = "http://127.0.0.1:19281"
TERMINAL_EXPORT_STATES = {"completed", "failed", "canceled", "expired"}
EVENT_TYPE_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9]*")
RESTART_KEY_CODES = {"insert": 0x2D, "capslock": 0x14}

if hasattr(sys.stdout, "reconfigure"):
	sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class ClientError(Exception):
	pass


class RejectRedirects(HTTPRedirectHandler):
	"""Prevent bearer credentials from following a redirect off loopback."""

	def redirect_request(self, request, fp, code, message, headers, new_url):
		return None


def emit(value):
	print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def validate_base_url(value):
	parsed = urlsplit(value.rstrip("/"))
	if (
		parsed.scheme != "http"
		or parsed.hostname != "127.0.0.1"
		or parsed.username
		or parsed.password
		or parsed.query
		or parsed.fragment
		or parsed.path not in ("", "/")
	):
		raise argparse.ArgumentTypeError("base URL must be http://127.0.0.1:<port>")
	try:
		port = parsed.port
	except ValueError as error:
		raise argparse.ArgumentTypeError("base URL has an invalid port") from error
	if port is None or not 1 <= port <= 65535:
		raise argparse.ArgumentTypeError("base URL must include a valid port")
	return "%s://%s:%d" % (parsed.scheme, parsed.hostname, port)


def nullable_integer(value):
	if value.lower() == "null":
		return None
	try:
		return int(value)
	except ValueError as error:
		raise argparse.ArgumentTypeError("expected an integer or null") from error


def event_types(value):
	items = [item.strip() for item in value.split(",") if item.strip()]
	if not items or len(items) > 20 or any(not EVENT_TYPE_PATTERN.fullmatch(item) for item in items):
		raise argparse.ArgumentTypeError("expected 1-20 comma-separated event type names")
	return ",".join(items)


def event_id(value):
	if not value or len(value) > 128 or any(ord(char) < 33 or ord(char) > 126 for char in value):
		raise argparse.ArgumentTypeError("Last-Event-ID must be 1-128 visible ASCII characters")
	return value


def response_status(response):
	status = getattr(response, "status", None)
	if status is None:
		status = response.getcode()
	return int(status)


class NvdaClient:
	def __init__(self, base_url, token_file, timeout):
		self.base_url = base_url
		self.token_file = token_file
		self.timeout = timeout
		# Never proxy or redirect a request carrying the local bearer token.
		self.opener = build_opener(ProxyHandler({}), RejectRedirects())

	def _read_token(self):
		if self.token_file is None:
			appdata = os.environ.get("APPDATA")
			if not appdata:
				raise ClientError("APPDATA is unavailable; pass --token-file")
			token_root = Path(appdata) / "nvda"
			paths = (token_root / "nvdaHttpBridge.token",)
		else:
			paths = (Path(self.token_file),)
		last_error = None
		for path in paths:
			try:
				token = path.read_text(encoding="utf-8-sig").strip()
			except OSError as error:
				last_error = error
				continue
			if token:
				return token
			raise ClientError("the NVDA HTTP token file is empty")
		raise ClientError("unable to read the NVDA HTTP token file") from last_error

	def _request(self, method, path, body=None, auth=False, accept="application/json", extra_headers=None):
		headers = {"Accept": accept}
		if extra_headers:
			headers.update(extra_headers)
		data = None
		if auth:
			headers["Authorization"] = "Bearer " + self._read_token()
		if body is not None:
			data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
			headers["Content-Type"] = "application/json"
		request = Request(self.base_url + path, data=data, headers=headers, method=method)
		try:
			return self.opener.open(request, timeout=self.timeout)
		except HTTPError as error:
			return error
		except (OSError, URLError) as error:
			raise ClientError("unable to reach the local NVDA HTTP bridge") from error

	@staticmethod
	def _decode_response(response):
		status = response_status(response)
		content_type = response.headers.get("Content-Type", "")
		try:
			try:
				raw = response.read()
			except IncompleteRead as error:
				raise ClientError("the local NVDA HTTP bridge interrupted its response") from error
		finally:
			response.close()
		if raw:
			try:
				data = json.loads(raw.decode("utf-8"))
			except (UnicodeDecodeError, json.JSONDecodeError):
				data = {"error": {"code": "invalidResponse", "message": "response was not JSON"}}
		else:
			data = None
		return {"httpStatus": status, "contentType": content_type, "data": data}

	def json(self, method, path, body=None, token_mode="none"):
		auth = token_mode == "required"
		result = self._decode_response(self._request(method, path, body=body, auth=auth))
		if token_mode == "optional" and result["httpStatus"] == 401:
			result = self._decode_response(self._request(method, path, body=body, auth=True))
		return result

	def download(self, path, output, accept="application/x-ndjson"):
		response = self._request("GET", path, auth=True, accept=accept)
		status = response_status(response)
		if status >= 400:
			return self._decode_response(response)
		content_type = response.headers.get("Content-Type", "")
		output = output.expanduser().resolve()
		if not output.parent.is_dir():
			response.close()
			raise ClientError("download parent directory does not exist")
		created = False
		try:
			with output.open("xb") as destination:
				created = True
				shutil.copyfileobj(response, destination, length=64 * 1024)
		except FileExistsError as error:
			raise ClientError("refusing to overwrite the download path") from error
		except Exception:
			if created:
				try:
					output.unlink()
				except OSError:
					pass
			raise
		finally:
			response.close()
		return {
			"httpStatus": status,
			"contentType": content_type,
			"data": {"output": str(output), "bytes": output.stat().st_size},
		}

	def events(self, types, duration, max_events, last_event_id=None):
		path = query_path("/v1/events", {"types": types})
		headers = {"Last-Event-ID": last_event_id} if last_event_id else None
		response = self._request(
			"GET",
			path,
			auth=True,
			accept="text/event-stream",
			extra_headers=headers,
		)
		status = response_status(response)
		if status >= 400:
			return self._decode_response(response)
		content_type = response.headers.get("Content-Type", "")

		started = time.monotonic()
		deadline = started + duration
		line_queue = queue.Queue()
		end_marker = object()

		def read_lines():
			try:
				while True:
					line = response.readline()
					if not line:
						break
					line_queue.put(line)
			except (OSError, ValueError, AttributeError):
				pass
			finally:
				line_queue.put(end_marker)

		reader = threading.Thread(target=read_lines, name="nvdaHttpBridgeSseReader", daemon=True)
		reader.start()
		events = []
		current = {"data": []}
		ended = "duration"
		try:
			while len(events) < max_events:
				remaining = deadline - time.monotonic()
				if remaining <= 0:
					break
				try:
					line = line_queue.get(timeout=min(0.25, remaining))
				except queue.Empty:
					continue
				if line is end_marker:
					ended = "server"
					break
				text_line = line.decode("utf-8", errors="replace").rstrip("\r\n")
				if not text_line:
					if current["data"]:
						raw_data = "\n".join(current["data"])
						try:
							data = json.loads(raw_data)
						except json.JSONDecodeError:
							data = raw_data
						event = {"event": current.get("event", "message"), "data": data}
						if "id" in current:
							event["id"] = current["id"]
						events.append(event)
					current = {"data": []}
					continue
				if text_line.startswith(":"):
					continue
				field, separator, value = text_line.partition(":")
				if separator and value.startswith(" "):
					value = value[1:]
				if field == "data":
					current["data"].append(value)
				elif field in ("event", "id"):
					current[field] = value
			if len(events) >= max_events:
				ended = "maxEvents"
		finally:
			# Closing a buffered HTTPResponse can wait for a concurrent readline.
			# Shut down the loopback socket first so the duration bound stays real.
			try:
				stream_socket = response.fp.raw._sock
				stream_socket.shutdown(socket.SHUT_RDWR)
				stream_socket.close()
			except (AttributeError, OSError):
				pass
			reader.join(timeout=0.5)
			if not reader.is_alive():
				response.close()

		return {
			"httpStatus": status,
			"contentType": content_type,
			"data": {
				"events": events,
				"count": len(events),
				"elapsedMs": int((time.monotonic() - started) * 1000),
				"ended": ended,
			},
		}


def query_path(path, values):
	params = {key: value for key, value in values.items() if value is not None}
	return path + ("?" + urlencode(params) if params else "")


def add_tree_arguments(parser, export=False):
	parser.add_argument("--root", choices=("focus", "foreground", "navigator", "desktop"), default="focus")
	if export:
		parser.add_argument("--depth", type=nullable_integer, default=3)
		parser.add_argument("--max-children", type=nullable_integer, default=20)
		parser.add_argument("--max-nodes", type=nullable_integer, default=200)
		parser.add_argument("--format", choices=("nested", "flat"), default="flat")
	else:
		parser.add_argument("--depth", type=int)
		parser.add_argument("--max-children", type=int)
		parser.add_argument("--max-nodes", type=int)
		parser.add_argument("--timeout-ms", type=int)
		parser.add_argument("--format", choices=("nested", "flat"))
	parser.add_argument("--include", help="comma-separated field allowlist")
	if export:
		parser.add_argument("--allow-unbounded", action="store_true")


def tree_params(args):
	return {
		"root": args.root,
		"depth": args.depth,
		"maxChildren": args.max_children,
		"maxNodes": args.max_nodes,
		"timeoutMs": args.timeout_ms,
		"format": args.format,
		"include": args.include,
	}


def export_body(args):
	body = {
		"root": args.root,
		"depth": args.depth,
		"maxChildren": args.max_children,
		"maxNodes": args.max_nodes,
		"format": args.format,
	}
	unbounded = any(body[key] is None for key in ("depth", "maxChildren", "maxNodes"))
	if unbounded and not args.allow_unbounded:
		raise ClientError("explicit null export limits require --allow-unbounded")
	if args.include:
		body["include"] = [item.strip() for item in args.include.split(",") if item.strip()]
	return body


def is_success(result):
	return 200 <= result["httpStatus"] < 300


def read_json_file(path):
	try:
		raw = path.read_bytes()
	except OSError as error:
		raise ClientError("unable to read JSON file: %s" % path) from error
	if len(raw) > 256 * 1024:
		raise ClientError("JSON file exceeds the 256 KiB request limit")
	try:
		return json.loads(raw.decode("utf-8-sig"))
	except (UnicodeDecodeError, json.JSONDecodeError) as error:
		raise ClientError("JSON file must contain valid UTF-8 JSON: %s" % path) from error


def reconcile_unknown_completion(client, result, get_path):
	error = (result.get("data") or {}).get("error") or {}
	details = error.get("details") or {}
	if result.get("httpStatus") == 504 and details.get("completionUnknown") is True:
		result["reconciliation"] = client.json("GET", get_path, token_mode="required")
	return result


def job_id_from(result):
	data = result.get("data") or {}
	job_id = data.get("jobId")
	if not job_id:
		raise ClientError("job response did not contain a jobId")
	return job_id


def send_restart_hotkey(nvda_key):
	"""Send the configured NVDA+Shift+Q shortcut outside the NVDA process."""
	if sys.platform != "win32":
		raise ClientError("restart is only supported on Windows")
	key_code = RESTART_KEY_CODES[nvda_key]
	keys = ((key_code, key_code == 0x2D), (0x10, False), (0x51, False))
	key_up = 0x0002
	key_extended = 0x0001
	pressed = []
	try:
		for virtual_key, extended in keys:
			ctypes.windll.user32.keybd_event(
				virtual_key,
				0,
				key_extended if extended else 0,
				0,
			)
			pressed.append((virtual_key, extended))
	finally:
		for virtual_key, extended in reversed(pressed):
			ctypes.windll.user32.keybd_event(
				virtual_key,
				0,
				key_up | (key_extended if extended else 0),
				0,
			)


def _healthy_uptime(result):
	if not is_success(result):
		return None
	data = result.get("data") or {}
	uptime = data.get("uptimeMs")
	if data.get("status") != "ok" or not isinstance(uptime, (int, float)) or uptime < 0:
		return None
	return float(uptime)


def run_restart(client, args, sender=send_restart_hotkey, clock=time.monotonic, sleep=time.sleep):
	baseline = client.json("GET", "/health")
	before_uptime = _healthy_uptime(baseline)
	if before_uptime is None:
		raise ClientError("NVDA HTTP health must be status ok with a numeric uptimeMs before restart")

	sender(args.nvda_key)
	started = clock()
	deadline = started + args.wait_seconds
	attempts = 0
	observed_unavailable = False
	last_result = None
	# A disappearing bridge should not consume the ordinary per-request timeout.
	client.timeout = min(client.timeout, 1.0)
	while clock() < deadline:
		attempts += 1
		try:
			last_result = client.json("GET", "/health")
		except ClientError:
			observed_unavailable = True
		else:
			after_uptime = _healthy_uptime(last_result)
			if after_uptime is not None and after_uptime < before_uptime:
				return {
					"httpStatus": 200,
					"data": {
						"status": "restarted",
						"hotkey": "NVDA+Shift+Q",
						"nvdaKey": args.nvda_key,
						"beforeUptimeMs": before_uptime,
						"afterUptimeMs": after_uptime,
						"observedUnavailable": observed_unavailable,
						"attempts": attempts,
						"elapsedMs": int((clock() - started) * 1000),
					},
				}, 0
		sleep(min(args.poll_interval, max(0, deadline - clock())))

	return {
		"httpStatus": 408,
		"data": {
			"error": {
				"code": "restartTimeout",
				"message": "NVDA HTTP did not return with a lower uptimeMs before the deadline",
			},
			"hotkey": "NVDA+Shift+Q",
			"nvdaKey": args.nvda_key,
			"beforeUptimeMs": before_uptime,
			"observedUnavailable": observed_unavailable,
			"attempts": attempts,
			"lastHealth": last_result,
		},
	}, 2


def run_backup(client, args):
	target = args.output.expanduser().resolve()
	output = target / "nvda"
	if output.exists():
		raise ClientError("refusing to overwrite the backup destination: %s" % output)

	created = client.json("POST", "/v1/backups", {"targetPath": str(target)}, "required")
	if not is_success(created):
		return created, 2
	job_id = job_id_from(created)
	terminal = None
	cleanup = None
	file_count = None
	backup_bytes = None
	try:
		deadline = time.monotonic() + args.wait_seconds
		while time.monotonic() < deadline:
			terminal = client.json("GET", "/v1/backups/" + quote(job_id, safe=""), token_mode="required")
			if not is_success(terminal):
				break
			if (terminal.get("data") or {}).get("status") in TERMINAL_EXPORT_STATES:
				break
			time.sleep(args.poll_interval)
		if terminal is None or (is_success(terminal) and (terminal.get("data") or {}).get("status") not in TERMINAL_EXPORT_STATES):
			terminal = {
				"httpStatus": 408,
				"data": {"error": {"code": "clientTimeout", "message": "backup polling timed out"}},
			}
		if is_success(terminal) and (terminal.get("data") or {}).get("status") == "completed":
			terminal_data = terminal.get("data") or {}
			server_output = terminal_data.get("backupPath")
			if not server_output:
				raise ClientError("backup server completed without returning backupPath")
			if os.path.normcase(os.path.abspath(server_output)) != os.path.normcase(str(output)):
				raise ClientError("backup server returned an unexpected backup path")
			if not output.is_dir():
				raise ClientError("backup server completed without creating the backup directory")
			file_count = terminal_data.get("fileCount")
			backup_bytes = terminal_data.get("bytes")
	finally:
		try:
			cleanup = client.json("DELETE", "/v1/backups/" + quote(job_id, safe=""), token_mode="required")
		except ClientError as error:
			cleanup = {
				"httpStatus": 0,
				"data": {"error": {"code": "clientError", "message": str(error)}},
			}

	terminal_state = (terminal.get("data") or {}).get("status")
	error = None
	result_status = 200
	if not is_success(terminal):
		result_status = terminal["httpStatus"]
		error = (terminal.get("data") or {}).get("error")
	elif terminal_state != "completed":
		result_status = 409
		error = (terminal.get("data") or {}).get("error") or {
			"code": "backupIncomplete",
			"message": "backup ended with status %s" % (terminal_state or "unknown"),
		}
	elif not cleanup or not is_success(cleanup):
		result_status = (cleanup or {}).get("httpStatus", 0)
		error = {"code": "cleanupFailed", "message": "backup succeeded but the server job was not deleted"}

	result = {
		"httpStatus": result_status,
		"data": {
			"jobId": job_id,
			"status": "backedUp" if error is None else terminal_state,
			"output": str(output) if error is None else None,
			"fileCount": file_count,
			"bytes": backup_bytes,
			"terminal": terminal.get("data"),
			"cleanup": cleanup,
		},
	}
	if error:
		result["data"]["error"] = error
	return result, 0 if error is None else 2


def run_export(client, args):
	created = client.json("POST", "/v1/tree/exports", export_body(args), "required")
	if not is_success(created):
		return created, 2
	job_id = job_id_from(created)
	terminal = None
	download = None
	cleanup = None
	try:
		deadline = time.monotonic() + args.wait_seconds
		while time.monotonic() < deadline:
			status = client.json("GET", "/v1/tree/exports/" + quote(job_id, safe=""), token_mode="required")
			if not is_success(status):
				terminal = status
				break
			state = (status.get("data") or {}).get("status")
			if state in TERMINAL_EXPORT_STATES:
				terminal = status
				break
			time.sleep(args.poll_interval)
		if terminal is None:
			terminal = {
				"httpStatus": 408,
				"data": {"error": {"code": "clientTimeout", "message": "export polling timed out"}},
			}
		if is_success(terminal) and (terminal.get("data") or {}).get("status") == "completed":
			try:
				download = client.download(
					"/v1/tree/exports/%s/data" % quote(job_id, safe=""),
					args.output,
				)
			except ClientError as error:
				download = {
					"httpStatus": 0,
					"data": {"error": {"code": "clientError", "message": str(error)}},
				}
	finally:
		if not args.keep_server_copy:
			try:
				cleanup = client.json(
					"DELETE",
					"/v1/tree/exports/" + quote(job_id, safe=""),
					token_mode="required",
				)
			except ClientError as error:
				cleanup = {
					"httpStatus": 0,
					"data": {"error": {"code": "clientError", "message": str(error)}},
				}

	terminal_state = (terminal.get("data") or {}).get("status")
	error = None
	result_status = 200
	if not is_success(terminal):
		result_status = terminal["httpStatus"]
		error = (terminal.get("data") or {}).get("error") or {
			"code": "exportStatusFailed",
			"message": "unable to read the export status",
		}
	elif terminal_state != "completed":
		result_status = terminal["httpStatus"] if terminal["httpStatus"] >= 400 else 409
		error = {
			"code": "exportIncomplete",
			"message": "export ended with status %s" % (terminal_state or "unknown"),
		}
	elif not download or not is_success(download):
		result_status = (download or {}).get("httpStatus", 0)
		error = ((download or {}).get("data") or {}).get("error") or {
			"code": "downloadFailed",
			"message": "export download failed",
		}
	elif not args.keep_server_copy and (not cleanup or not is_success(cleanup)):
		result_status = (cleanup or {}).get("httpStatus", 0)
		error = {
			"code": "cleanupFailed",
			"message": "download succeeded but the server-side export could not be deleted",
		}

	result = {
		"httpStatus": result_status,
		"data": {
			"jobId": job_id,
			"terminal": terminal.get("data"),
			"download": download,
			"cleanup": cleanup,
		},
	}
	if error:
		result["data"]["error"] = error
	return result, 0 if error is None else 2


def build_parser():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--base-url", type=validate_base_url, default=DEFAULT_BASE_URL)
	parser.add_argument("--token-file", type=Path)
	parser.add_argument("--timeout", type=float, default=8.0)
	sub = parser.add_subparsers(dest="command", required=True)

	for command in ("health", "version", "capabilities", "cancel-speech"):
		sub.add_parser(command)

	obj = sub.add_parser("object")
	obj.add_argument("root", choices=("focus", "foreground", "navigator", "desktop"))
	obj.add_argument("--include")

	obj_id = sub.add_parser("object-id")
	obj_id.add_argument("object_id")
	obj_id.add_argument("--include")

	tree = sub.add_parser("tree")
	add_tree_arguments(tree)

	for command in ("speech-history", "log-tail"):
		history = sub.add_parser(command)
		history.add_argument("--last", type=int, default=20)

	events = sub.add_parser("events")
	events.add_argument("--types", type=event_types, default="gainFocus,speech")
	events.add_argument("--duration", type=float, default=5.0)
	events.add_argument("--max-events", type=int, default=50)
	events.add_argument("--last-event-id", type=event_id)

	create = sub.add_parser("export-create")
	add_tree_arguments(create, export=True)
	for command in ("export-status", "export-cancel"):
		job = sub.add_parser(command)
		job.add_argument("job_id")
	download = sub.add_parser("export-download")
	download.add_argument("job_id")
	download.add_argument("--output", type=Path, required=True)
	run = sub.add_parser("export-run")
	add_tree_arguments(run, export=True)
	run.add_argument("--output", type=Path, required=True)
	run.add_argument("--wait-seconds", type=float, default=60.0)
	run.add_argument("--poll-interval", type=float, default=0.2)
	run.add_argument("--keep-server-copy", action="store_true")

	speak = sub.add_parser("speak")
	speak.add_argument("text")
	gesture = sub.add_parser("gesture")
	gesture.add_argument("key")
	restart = sub.add_parser("restart")
	restart.add_argument("--nvda-key", choices=tuple(RESTART_KEY_CODES), default="insert")
	restart.add_argument("--wait-seconds", type=float, default=30.0)
	restart.add_argument("--poll-interval", type=float, default=0.25)
	backup_create = sub.add_parser("backup-create")
	backup_create.add_argument("--output", type=Path, required=True)
	for command in ("backup-status", "backup-cancel"):
		backup_job = sub.add_parser(command)
		backup_job.add_argument("job_id")
	backup = sub.add_parser("backup")
	backup.add_argument("--output", type=Path, default=Path("."))
	backup.add_argument("--wait-seconds", type=float, default=900.0)
	backup.add_argument("--poll-interval", type=float, default=0.5)
	for command in ("focus-object", "default-action"):
		action = sub.add_parser(command)
		action.add_argument("object_id")
		action.add_argument("--generation")

	for command in ("settings-categories", "settings-get", "speech-dictionaries"):
		sub.add_parser(command)
	settings_set = sub.add_parser("settings-set")
	settings_set.add_argument("--body-file", type=Path, required=True)
	for command in ("speech-dictionary-get", "speech-dictionary-validate", "speech-dictionary-put"):
		dictionary = sub.add_parser(command)
		dictionary.add_argument("dictionary_id", choices=("default", "voice", "temp"))
		if command != "speech-dictionary-get":
			dictionary.add_argument("--body-file", type=Path, required=True)
	symbols_get = sub.add_parser("symbols-get")
	symbols_get.add_argument("locale", default="current", nargs="?")
	symbols_put = sub.add_parser("symbols-put")
	symbols_put.add_argument("locale")
	symbols_put.add_argument("--body-file", type=Path, required=True)
	gestures_get = sub.add_parser("gestures-get")
	gestures_get.add_argument("--filter")
	gestures_patch = sub.add_parser("gestures-patch")
	gestures_patch.add_argument("--body-file", type=Path, required=True)
	return parser


def execute(client, args):
	command = args.command
	if command in ("health", "version", "capabilities"):
		return client.json("GET", "/health" if command == "health" else "/v1/" + command), None
	if command == "object":
		path = query_path("/v1/objects/" + args.root, {"include": args.include})
		return client.json("GET", path, token_mode="optional"), None
	if command == "object-id":
		path = query_path("/v1/objects/by-id/" + quote(args.object_id, safe=""), {"include": args.include})
		return client.json("GET", path, token_mode="optional"), None
	if command == "tree":
		return client.json("GET", query_path("/v1/tree", tree_params(args)), token_mode="optional"), None
	if command in ("speech-history", "log-tail"):
		path = "/v1/speech" if command == "speech-history" else "/v1/log"
		return client.json("GET", query_path(path, {"last": args.last}), token_mode="required"), None
	if command == "events":
		return client.events(args.types, args.duration, args.max_events, args.last_event_id), None
	if command == "export-create":
		return client.json("POST", "/v1/tree/exports", export_body(args), "required"), None
	if command == "export-status":
		return client.json("GET", "/v1/tree/exports/" + quote(args.job_id, safe=""), token_mode="required"), None
	if command == "export-cancel":
		return client.json("DELETE", "/v1/tree/exports/" + quote(args.job_id, safe=""), token_mode="required"), None
	if command == "export-download":
		return client.download("/v1/tree/exports/%s/data" % quote(args.job_id, safe=""), args.output), None
	if command == "export-run":
		return run_export(client, args)
	if command == "speak":
		return client.json("POST", "/v1/actions/speak", {"text": args.text}, "required"), None
	if command == "cancel-speech":
		return client.json("POST", "/v1/actions/cancel-speech", {}, "required"), None
	if command == "gesture":
		return client.json("POST", "/v1/actions/gesture", {"key": args.key}, "required"), None
	if command == "restart":
		return run_restart(client, args)
	if command == "backup-create":
		return client.json(
			"POST",
			"/v1/backups",
			{"targetPath": str(args.output.expanduser().resolve())},
			"required",
		), None
	if command == "backup-status":
		return client.json("GET", "/v1/backups/" + quote(args.job_id, safe=""), token_mode="required"), None
	if command == "backup-cancel":
		return client.json("DELETE", "/v1/backups/" + quote(args.job_id, safe=""), token_mode="required"), None
	if command == "backup":
		return run_backup(client, args)
	if command in ("focus-object", "default-action"):
		body = {"objectId": args.object_id}
		if args.generation:
			body["generation"] = args.generation
		action = "focus" if command == "focus-object" else "default-action"
		return client.json("POST", "/v1/actions/" + action, body, "required"), None
	if command == "settings-categories":
		return client.json("GET", "/v1/settings/categories", token_mode="required"), None
	if command == "settings-get":
		return client.json("GET", "/v1/settings/general", token_mode="required"), None
	if command == "settings-set":
		result = client.json("PATCH", "/v1/settings/general", read_json_file(args.body_file), "required")
		return reconcile_unknown_completion(client, result, "/v1/settings/general"), None
	if command == "speech-dictionaries":
		return client.json("GET", "/v1/speech-dictionaries", token_mode="required"), None
	if command == "speech-dictionary-get":
		path = "/v1/speech-dictionaries/" + quote(args.dictionary_id, safe="")
		return client.json("GET", path, token_mode="required"), None
	if command in ("speech-dictionary-validate", "speech-dictionary-put"):
		path = "/v1/speech-dictionaries/" + quote(args.dictionary_id, safe="")
		method = "POST" if command.endswith("validate") else "PUT"
		if method == "POST":
			path += "/validate"
		result = client.json(method, path, read_json_file(args.body_file), "required")
		if method == "PUT":
			result = reconcile_unknown_completion(client, result, path)
		return result, None
	if command == "symbols-get":
		path = "/v1/symbol-dictionaries/" + quote(args.locale, safe="")
		return client.json("GET", path, token_mode="required"), None
	if command == "symbols-put":
		path = "/v1/symbol-dictionaries/" + quote(args.locale, safe="")
		result = client.json("PUT", path, read_json_file(args.body_file), "required")
		return reconcile_unknown_completion(client, result, path), None
	if command == "gestures-get":
		path = query_path("/v1/gestures", {"context": "current", "filter": args.filter})
		return client.json("GET", path, token_mode="required"), None
	if command == "gestures-patch":
		result = client.json("PATCH", "/v1/gestures", read_json_file(args.body_file), "required")
		return reconcile_unknown_completion(client, result, "/v1/gestures?context=current"), None
	raise ClientError("unsupported command")


def main(argv=None):
	parser = build_parser()
	args = parser.parse_args(argv)
	if args.timeout <= 0:
		parser.error("--timeout must be positive")
	if hasattr(args, "wait_seconds") and args.wait_seconds <= 0:
		parser.error("--wait-seconds must be positive")
	if hasattr(args, "poll_interval") and not 0.05 <= args.poll_interval <= 5:
		parser.error("--poll-interval must be between 0.05 and 5 seconds")
	if hasattr(args, "duration") and not 0.1 <= args.duration <= 60:
		parser.error("--duration must be between 0.1 and 60 seconds")
	if hasattr(args, "max_events") and not 1 <= args.max_events <= 1000:
		parser.error("--max-events must be between 1 and 1000")
	client = NvdaClient(args.base_url, args.token_file, args.timeout)
	try:
		result, explicit_exit = execute(client, args)
	except ClientError as error:
		emit({"httpStatus": 0, "data": {"error": {"code": "clientError", "message": str(error)}}})
		return 1
	except KeyboardInterrupt:
		emit({"httpStatus": 0, "data": {"error": {"code": "interrupted", "message": "operation interrupted"}}})
		return 130
	emit(result)
	if explicit_exit is not None:
		return explicit_exit
	return 0 if is_success(result) else 2


if __name__ == "__main__":
	sys.exit(main())
