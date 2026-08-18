"""Versioned bridge service independent of the HTTP implementation."""

from collections import deque
import json
import os
import sys
import threading
import time
import uuid

from .auth import RateLimiter
from .config import (
	ALLOWED_FIELDS,
	BACKUP_CREATE_RATE_LIMIT,
	BACKUP_CREATE_RATE_WINDOW_SECONDS,
	BACKUP_MAX_BYTES,
	BACKUP_MAX_CONCURRENT,
	BACKUP_MAX_DURATION_SECONDS,
	BACKUP_TTL_SECONDS,
	BLOCKED_GESTURE_CHORDS,
	DEFAULT_FIELDS,
	DEFAULT_TREE_CHILDREN,
	DEFAULT_TREE_DEPTH,
	DEFAULT_TREE_NODES,
	DEFAULT_TREE_TIMEOUT_MS,
	DIAGNOSTICS_CREATE_RATE_LIMIT,
	DIAGNOSTICS_CREATE_RATE_WINDOW_SECONDS,
	DIAGNOSTICS_MAX_BYTES,
	DIAGNOSTICS_MAX_CONCURRENT,
	DIAGNOSTICS_MAX_RETAINED_JOBS,
	DIAGNOSTICS_TTL_SECONDS,
	EXPORT_CREATE_RATE_LIMIT,
	EXPORT_CREATE_RATE_WINDOW_SECONDS,
	EXPORT_MAX_BYTES,
	EXPORT_MAX_CHILDREN,
	EXPORT_MAX_DEPTH,
	EXPORT_MAX_DURATION_SECONDS,
	EXPORT_MAX_NODES,
	EXPORT_MAX_RETAINED_JOBS,
	EXPORT_MAX_TOTAL_BYTES,
	EXPORT_TTL_SECONDS,
	HOST,
	MAX_ACTIVE_REQUESTS,
	MAX_CONCURRENT_SYNC_TREES,
	MAX_CONNECTIONS,
	MAX_HEADER_BYTES,
	MAX_REQUEST_BODY,
	MAX_REQUEST_LINE_BYTES,
	MAX_SSE_CLIENTS,
	PLUGIN_VERSION,
	PORT,
	PROTOCOL_VERSION,
	ROOT_NAMES,
	SYNC_BATCH_BUDGET_MS,
	SYNC_BATCH_NODES,
	SYNC_MAX_CHILDREN,
	SYNC_MAX_DEPTH,
	SYNC_MAX_NODES,
	SYNC_MAX_RESULT_BYTES,
	SYNC_MAX_TIMEOUT_MS,
	TREE_FORMATS,
)
from .errors import (
	BadRequest,
	NotFound,
	RestartAlreadyScheduled,
	SecureContext,
	TooManyRequests,
	UnsafeAction,
	ValidationError,
)
from .serialization import safe_text, serialize_object
from .tree import TreeWalker, _fields, parse_export_options, parse_sync_options, tree_result


class BridgeService:
	def __init__(
		self,
		adapter,
		executor,
		registry,
		events,
		speech_observer,
		exports,
		security_state,
		backups=None,
		settings=None,
		status=None,
		text=None,
		diagnostics=None,
		diagnostic_exports=None,
		speech_dictionaries=None,
		symbol_dictionaries=None,
		gestures=None,
		monotonic=None,
		logger=None,
	):
		self.adapter = adapter
		self.executor = executor
		self.registry = registry
		self.events = events
		self.speech_observer = speech_observer
		self.exports = exports
		self.security_state = security_state
		self.backups = backups
		self.settings = settings
		self.status_adapter = status
		self.text_adapter = text
		self.diagnostics_adapter = diagnostics
		self.diagnostic_exports = diagnostic_exports
		self.speech_dictionaries = speech_dictionaries
		self.symbol_dictionaries = symbol_dictionaries
		self.gestures = gestures
		self._monotonic = monotonic or time.monotonic
		self._logger = logger
		self._started_at = self._monotonic()
		self._closing = False
		self._closed = False
		self._rate_limiter = RateLimiter()
		self._export_rate_limiter = RateLimiter(
			limit=EXPORT_CREATE_RATE_LIMIT,
			window_seconds=EXPORT_CREATE_RATE_WINDOW_SECONDS,
			monotonic=self._monotonic,
		)
		self._backup_rate_limiter = RateLimiter(
			limit=BACKUP_CREATE_RATE_LIMIT,
			window_seconds=BACKUP_CREATE_RATE_WINDOW_SECONDS,
			monotonic=self._monotonic,
		)
		self._diagnostics_rate_limiter = RateLimiter(
			limit=DIAGNOSTICS_CREATE_RATE_LIMIT,
			window_seconds=DIAGNOSTICS_CREATE_RATE_WINDOW_SECONDS,
			monotonic=self._monotonic,
		)
		self._tree_slots = threading.BoundedSemaphore(MAX_CONCURRENT_SYNC_TREES)
		self._restart_lock = threading.Lock()
		self._pending_restart = None
		self._restart_dispatched = False
		self._event_generation = self.registry.new_generation()

	def assert_data_available(self):
		if self._closing:
			from .errors import ServiceUnavailable

			raise ServiceUnavailable()
		if self.security_state.restricted():
			raise SecureContext()

	def health(self):
		executor_metrics = self.executor.metrics()
		last_timeout = executor_metrics.get("lastTimeoutMonotonic")
		last_execution = executor_metrics.get("lastExecutionMonotonic")
		if last_timeout is not None and (last_execution is None or last_timeout > last_execution):
			main_thread_state = "unavailable"
		elif last_execution is not None:
			main_thread_state = "available"
		else:
			main_thread_state = "unknown"
		status = "closing" if self._closing else ("degraded" if main_thread_state == "unavailable" else "ok")
		identity = self.adapter.nvda_identity()
		with self._restart_lock:
			restart_pending = self._pending_restart is not None
		return {
			"status": status,
			"protocolVersion": PROTOCOL_VERSION,
			"pluginVersion": PLUGIN_VERSION,
			"uptimeMs": round((self._monotonic() - self._started_at) * 1000, 2),
			"nvdaProcessId": identity["nvdaProcessId"],
			"nvdaStartTime": identity["nvdaStartTime"],
			"restartPending": restart_pending,
			"security": self.security_state.snapshot(),
			"mainThread": {"state": main_thread_state},
			"executor": executor_metrics,
			"exports": self.exports.metrics(),
			"backups": self.backups.metrics() if self.backups is not None else None,
			"diagnosticExports": self.diagnostic_exports.metrics() if self.diagnostic_exports is not None else None,
			"objectRegistrySize": self.registry.size(),
		}

	def version(self):
		identity = self.adapter.nvda_identity()
		return {
			"plugin": {"name": "nvdaHttpBridge", "version": PLUGIN_VERSION},
			"protocolVersion": PROTOCOL_VERSION,
			"nvda": self.adapter.version(),
			"nvdaProcessId": identity["nvdaProcessId"],
			"nvdaStartTime": identity["nvdaStartTime"],
			"python": sys.version.split()[0],
		}

	def capabilities(self):
		return {
			"protocolVersion": PROTOCOL_VERSION,
			"listen": {"host": HOST, "port": PORT, "loopbackOnly": True},
			"auth": {"mode": "none"},
			"roots": list(ROOT_NAMES),
			"fields": list(ALLOWED_FIELDS),
			"treeFormats": list(TREE_FORMATS),
			"treeDefaults": {
				"depth": DEFAULT_TREE_DEPTH,
				"maxChildren": DEFAULT_TREE_CHILDREN,
				"maxNodes": DEFAULT_TREE_NODES,
				"timeoutMs": DEFAULT_TREE_TIMEOUT_MS,
			},
			"treeSyncMaximums": {
				"depth": SYNC_MAX_DEPTH,
				"maxChildren": SYNC_MAX_CHILDREN,
				"maxNodes": SYNC_MAX_NODES,
				"timeoutMs": SYNC_MAX_TIMEOUT_MS,
				"concurrentRequests": MAX_CONCURRENT_SYNC_TREES,
				"batchNodes": SYNC_BATCH_NODES,
				"batchBudgetMs": SYNC_BATCH_BUDGET_MS,
				"resultBytes": SYNC_MAX_RESULT_BYTES,
			},
			"transportLimits": {
				"connections": MAX_CONNECTIONS,
				"activeRequests": MAX_ACTIVE_REQUESTS,
				"sseClients": MAX_SSE_CLIENTS,
				"requestBodyBytes": MAX_REQUEST_BODY,
				"headerBytes": MAX_HEADER_BYTES,
				"requestLineBytes": MAX_REQUEST_LINE_BYTES,
			},
			"treeExportEmergencyLimits": {
				"depth": EXPORT_MAX_DEPTH,
				"maxChildren": EXPORT_MAX_CHILDREN,
				"maxNodes": EXPORT_MAX_NODES,
				"maxBytes": EXPORT_MAX_BYTES,
				"maxTotalBytes": EXPORT_MAX_TOTAL_BYTES,
				"maxRetainedJobs": EXPORT_MAX_RETAINED_JOBS,
				"createRateLimit": EXPORT_CREATE_RATE_LIMIT,
				"createRateWindowSeconds": EXPORT_CREATE_RATE_WINDOW_SECONDS,
				"maxDurationSeconds": EXPORT_MAX_DURATION_SECONDS,
				"retentionSeconds": EXPORT_TTL_SECONDS,
			},
			"backupLimits": {
				"targetPathChildName": "nvda",
				"maxConcurrent": BACKUP_MAX_CONCURRENT,
				"maxBytes": BACKUP_MAX_BYTES,
				"createRateLimit": BACKUP_CREATE_RATE_LIMIT,
				"createRateWindowSeconds": BACKUP_CREATE_RATE_WINDOW_SECONDS,
				"maxDurationSeconds": BACKUP_MAX_DURATION_SECONDS,
				"jobStatusRetentionSeconds": BACKUP_TTL_SECONDS,
				"completedBackupPreserved": True,
			},
			"textLimits": self.text_adapter.limits() if self.text_adapter is not None else None,
			"diagnosticExportLimits": {
				"maxConcurrent": DIAGNOSTICS_MAX_CONCURRENT,
				"maxBytes": DIAGNOSTICS_MAX_BYTES,
				"maxRetainedJobs": DIAGNOSTICS_MAX_RETAINED_JOBS,
				"createRateLimit": DIAGNOSTICS_CREATE_RATE_LIMIT,
				"createRateWindowSeconds": DIAGNOSTICS_CREATE_RATE_WINDOW_SECONDS,
				"retentionSeconds": DIAGNOSTICS_TTL_SECONDS,
			},
			"endpoints": {
				"status": "/v1/status",
				"modes": "/v1/modes",
				"focus": "/v1/objects/focus",
				"objectById": "/v1/objects/by-id/{objectId}",
				"tree": "/v1/tree",
				"treeExports": "/v1/tree/exports",
				"backups": "/v1/backups",
				"textCaret": "/v1/text/caret",
				"textSelection": "/v1/text/selection",
				"textObject": "/v1/text/object/{objectId}",
				"addons": "/v1/addons",
				"globalPlugins": "/v1/global-plugins",
				"drivers": "/v1/drivers",
				"diagnostics": "/v1/diagnostics",
				"diagnosticExports": "/v1/diagnostics/exports",
				"events": "/v1/events",
				"settings": "/v1/settings/general",
				"speechDictionaries": "/v1/speech-dictionaries",
				"symbolDictionaries": "/v1/symbol-dictionaries/{locale}",
				"gestures": "/v1/gestures",
				"lifecycleRestart": "/v1/lifecycle/restart",
			},
			"lifecycle": {
				"restart": {
					"endpoint": "/v1/lifecycle/restart",
					"execution": "processBoundaryAsync",
					"responseStatus": 202,
					"requestBody": "emptyObject",
					"verification": "nvdaProcessIdentity",
					"legacyFallback": "externalNvdaShiftQWhenCapabilityAbsent",
				},
			},
			"configurationResources": {
				"modes": {
					"execution": "synchronous",
					"writableFields": ["inputHelp", "sleepMode", "browseMode"],
					"readOnlyFields": ["screenCurtain"],
					"persistedByEndpoint": False,
				},
				"settings/general": {
					"execution": "synchronous",
					"fields": [
						"language", "saveConfigurationOnExit", "askToExit",
						"playStartAndExitSounds", "preventDisplayTurningOff",
					],
					"persistedByEndpoint": False,
					"restartFields": ["language"],
				},
				"speechDictionaries": {
					"execution": "synchronous",
					"ids": ["default", "voice", "temp"],
					"clearAllSupported": False,
				},
				"symbolDictionaries": {
					"execution": "synchronous",
					"actions": ["add", "edit", "removeUserOverride"],
				},
				"gestures": {
					"execution": "synchronous",
					"actions": ["add", "remove", "unbind", "addKbEmulation"],
					"resetAllSupported": False,
				},
			},
			"actions": [
				"speak", "cancel-speech", "gesture", "focus", "default-action",
				"set-caret", "set-selection",
			],
			"eventTypes": ["gainFocus", "foreground", "nameChange", "valueChange", "stateChange", "caret", "speech"],
		}

	def _configuration_call(self, adapter, method_name, *args, timeout_ms=3000):
		self.assert_data_available()
		if adapter is None:
			from .errors import ServiceUnavailable

			raise ServiceUnavailable("The requested configuration resource is unavailable")
		return self.executor.call(lambda: getattr(adapter, method_name)(*args), timeout_ms)

	def settings_categories(self):
		return self._configuration_call(self.settings, "categories")

	def runtime_status(self):
		return self._configuration_call(self.status_adapter, "get_status")

	def modes(self):
		return self._configuration_call(self.status_adapter, "get_modes")

	def patch_modes(self, body):
		return self._configuration_call(self.status_adapter, "patch_modes", body)

	def general_settings(self):
		return self._configuration_call(self.settings, "get_general")

	def patch_general_settings(self, body):
		return self._configuration_call(self.settings, "patch_general", body)

	def speech_dictionary_list(self):
		return self._configuration_call(self.speech_dictionaries, "list")

	def speech_dictionary(self, dictionary_id):
		return self._configuration_call(self.speech_dictionaries, "get", dictionary_id)

	def validate_speech_dictionary(self, dictionary_id, body):
		return self._configuration_call(self.speech_dictionaries, "validate", dictionary_id, body)

	def put_speech_dictionary(self, dictionary_id, body):
		return self._configuration_call(self.speech_dictionaries, "put", dictionary_id, body)

	def symbol_dictionary(self, locale):
		return self._configuration_call(self.symbol_dictionaries, "get", locale)

	def put_symbol_dictionary(self, locale, body):
		return self._configuration_call(self.symbol_dictionaries, "put", locale, body)

	def gesture_mappings(self, context="current", filter_text=None):
		if context != "current":
			raise ValidationError("Only context=current is supported")
		return self._configuration_call(self.gestures, "get", filter_text)

	def patch_gestures(self, body):
		return self._configuration_call(self.gestures, "patch", body)

	def object_snapshot(self, root_name, params=None):
		self.assert_data_available()
		if root_name not in ROOT_NAMES:
			raise ValidationError("Unknown root object")
		include = self._object_fields(params)

		def work():
			self.adapter.assert_safe()
			obj = self.adapter.get_root(root_name)
			self.adapter.assert_safe(obj)
			generation = self.registry.new_generation()
			return serialize_object(obj, include, self.registry, generation, self.adapter)

		return self.executor.call(work, 1000)

	def object_by_id(self, object_id, params=None):
		self.assert_data_available()
		include = self._object_fields(params)

		def work():
			obj, generation = self.registry.resolve(object_id)
			self.adapter.assert_safe(obj)
			return serialize_object(obj, include, self.registry, generation, self.adapter)

		return self.executor.call(work, 1000)

	def current_text(self, position, params=None):
		self.assert_data_available()
		if self.text_adapter is None:
			from .errors import ServiceUnavailable

			raise ServiceUnavailable("Text support is unavailable")
		offset, max_chars = self.text_adapter.parse_window(params)
		if offset:
			raise ValidationError("offset is only supported for object text")

		def work():
			self.adapter.assert_safe()
			obj = self.text_adapter.backend.caret_object()
			self.adapter.assert_safe(obj)
			generation = self.registry.new_generation()
			object_id = self.registry.register(obj, generation)
			return self.text_adapter.current(position, obj, object_id, generation, max_chars)

		return self.executor.call(work, 1000)

	def object_text(self, object_id, params=None):
		self.assert_data_available()
		if self.text_adapter is None:
			from .errors import ServiceUnavailable

			raise ServiceUnavailable("Text support is unavailable")
		offset, max_chars = self.text_adapter.parse_window(params)

		def work():
			obj, generation = self.registry.resolve(object_id)
			self.adapter.assert_safe(obj)
			return self.text_adapter.object_text(obj, object_id, generation, offset, max_chars)

		return self.executor.call(work, 3000)

	def addons(self):
		return self._configuration_call(self.diagnostics_adapter, "addons", timeout_ms=5000)

	def global_plugins(self):
		return self._configuration_call(self.diagnostics_adapter, "global_plugins", timeout_ms=5000)

	def drivers(self):
		return self._configuration_call(self.diagnostics_adapter, "drivers", timeout_ms=5000)

	def diagnostics(self):
		result = self._configuration_call(self.diagnostics_adapter, "snapshot", timeout_ms=5000)
		result["bridge"] = self.health()
		return result

	@staticmethod
	def _object_fields(params):
		params = params or {}
		unknown = sorted(set(params) - {"include"})
		if unknown:
			raise ValidationError("Unknown query parameters", details={"parameters": unknown})
		include_value = params.get("include")
		if isinstance(include_value, (list, tuple)):
			if len(include_value) != 1:
				raise ValidationError("Parameter 'include' must be supplied once")
			include_value = include_value[0]
		return _fields(include_value)

	def tree(self, params):
		self.assert_data_available()
		options = parse_sync_options(params)
		if not self._tree_slots.acquire(blocking=False):
			raise TooManyRequests("The synchronous tree request limit was reached")
		try:
			return self._tree_sliced(options)
		finally:
			self._tree_slots.release()

	def _tree_sliced(self, options):
		started = self._monotonic()
		deadline = started + options.timeout_ms / 1000.0

		def prepare():
			self.adapter.assert_safe()
			root = self.adapter.get_root(options.root)
			self.adapter.assert_safe(root)
			return root

		root = self.executor.call(prepare, options.timeout_ms)
		generation = self.registry.new_generation()
		walker = TreeWalker(root, options, self.registry, generation, self.adapter, self._monotonic)
		records = []
		result_bytes = 0
		while not walker.done:
			remaining_ms = int(max(0.0, deadline - self._monotonic()) * 1000)
			if remaining_ms <= 0:
				walker.abort("timeLimit")
				break
			remaining_nodes = options.max_nodes - walker.node_count

			def next_batch():
				# Security state can change while a multi-batch request is running.
				self.adapter.assert_safe(root)
				return walker.next_batch(
					min(SYNC_BATCH_NODES, max(1, remaining_nodes)),
					min(SYNC_BATCH_BUDGET_MS, remaining_ms),
				)

			batch = self.executor.call(next_batch, remaining_ms)
			for record in batch:
				record_bytes = len(
					json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
				)
				if result_bytes + record_bytes > SYNC_MAX_RESULT_BYTES:
					walker.abort("sizeLimit")
					break
				records.append(record)
				result_bytes += record_bytes
		return tree_result(records, walker, options, generation, started, self._monotonic)

	def speech(self, last=None):
		self.assert_data_available()
		if last is not None:
			try:
				last = int(last)
			except (TypeError, ValueError):
				raise ValidationError("Parameter 'last' must be an integer")
			if not 0 <= last <= 100:
				raise ValidationError("Parameter 'last' must be between 0 and 100")
		return {"items": self.speech_observer.history(last)}

	def log_tail(self, last=50):
		self.assert_data_available()
		try:
			last = int(last)
		except (TypeError, ValueError):
			raise ValidationError("Parameter 'last' must be an integer")
		if not 1 <= last <= 1000:
			raise ValidationError("Parameter 'last' must be between 1 and 1000")
		path = self.adapter.log_path
		if not path or not os.path.isfile(path):
			raise NotFound("The NVDA log file is unavailable")
		lines = deque(maxlen=last)
		with open(path, "r", encoding="utf-8", errors="replace") as log_file:
			for line in log_file:
				lines.append(line.rstrip("\r\n"))
		return {"lines": list(lines)}

	def create_export(self, body):
		self.assert_data_available()
		self._export_rate_limiter.check("create")
		return self.exports.create(parse_export_options(body))

	def export_status(self, job_id):
		self.assert_data_available()
		return self.exports.status(job_id)

	def export_data_path(self, job_id):
		self.assert_data_available()
		return self.exports.data_path(job_id)

	def open_export_data(self, job_id):
		self.assert_data_available()
		return self.exports.open_data(job_id)

	def export_download_allowed(self, job_id):
		return (
			not self._closing
			and not self.security_state.restricted()
			and self.exports.is_downloadable(job_id)
		)

	def cancel_export(self, job_id):
		return self.exports.cancel(job_id)

	def create_backup(self, body):
		self.assert_data_available()
		if not isinstance(body, dict):
			raise BadRequest("The backup body must be a JSON object")
		unknown = sorted(set(body) - {"targetPath"})
		if unknown:
			raise ValidationError("Unknown backup fields", details={"unknown": unknown})
		target_path = body.get("targetPath")
		if not isinstance(target_path, str) or not target_path.strip():
			raise ValidationError("targetPath must be a non-empty string")
		if self.backups is None:
			from .errors import ServiceUnavailable

			raise ServiceUnavailable("NVDA backup support is unavailable")
		self._backup_rate_limiter.check("create")
		return self.backups.create(target_path)

	def backup_status(self, job_id):
		self.assert_data_available()
		return self.backups.status(job_id)

	def cancel_backup(self, job_id):
		return self.backups.cancel(job_id)

	def create_diagnostic_export(self, body):
		self.assert_data_available()
		if not isinstance(body, dict):
			raise BadRequest("The diagnostic export body must be a JSON object")
		if body:
			raise ValidationError("The diagnostic export body must be an empty object")
		if self.diagnostic_exports is None:
			from .errors import ServiceUnavailable

			raise ServiceUnavailable("Diagnostic export support is unavailable")
		self._diagnostics_rate_limiter.check("create")
		return self.diagnostic_exports.create()

	def diagnostic_export_status(self, job_id):
		return self.diagnostic_exports.status(job_id)

	def open_diagnostic_export_data(self, job_id):
		self.assert_data_available()
		return self.diagnostic_exports.open_data(job_id)

	def diagnostic_export_download_allowed(self, job_id):
		return (
			not self._closing
			and not self.security_state.restricted()
			and self.diagnostic_exports.is_downloadable(job_id)
		)

	def cancel_diagnostic_export(self, job_id):
		return self.diagnostic_exports.cancel(job_id)

	def prepare_restart(self, body):
		self.assert_data_available()
		if not isinstance(body, dict):
			raise ValidationError("The restart body must be an empty JSON object")
		if body:
			raise ValidationError("The restart body must be an empty JSON object", details={"unknown": sorted(body)})
		self._rate_limiter.check("restart")
		self.executor.call(self.adapter.assert_restart_allowed, 1000)
		identity = self.adapter.nvda_identity()
		before = {
			"nvdaProcessId": identity["nvdaProcessId"],
			"nvdaStartTime": identity["nvdaStartTime"],
			"bridgeUptimeMs": round((self._monotonic() - self._started_at) * 1000, 2),
		}
		restart_id = uuid.uuid4().hex
		with self._restart_lock:
			if self._pending_restart is not None:
				raise RestartAlreadyScheduled()
			self._pending_restart = restart_id
		return {"status": "accepted", "restartId": restart_id, "before": before}

	def schedule_prepared_restart(self, restart_id):
		with self._restart_lock:
			if self._pending_restart != restart_id or self._restart_dispatched:
				return False
			self._restart_dispatched = True
			# Keep the reservation set until this process exits. A second request must
			# never schedule another lifecycle action if shutdown is slow or fails.
		def restart_work():
			try:
				self.adapter.restart()
			except Exception:
				if self._logger is not None:
					self._logger.exception(
						"nvdaHttpBridge: native restart failed restartId=%s",
						restart_id,
					)
				else:
					raise

		self.adapter.schedule(restart_work)
		return True

	def action(self, action_name, body):
		self.assert_data_available()
		if not isinstance(body, dict):
			raise BadRequest("The action body must be a JSON object")
		self._rate_limiter.check(action_name)
		if action_name == "restart":
			raise UnsafeAction("Plugin reload cannot be initiated from an in-flight HTTP request")
		if action_name == "speak":
			self._validate_keys(body, {"text"})
			text = safe_text(body.get("text"), 8192)
			if not text:
				raise ValidationError("A non-empty 'text' value is required")
			return self._main_action(lambda: self.adapter.speak(text), {"ok": True, "text": text})
		if action_name == "cancel-speech":
			self._validate_keys(body, set())
			return self._main_action(self.adapter.cancel_speech, {"ok": True})
		if action_name == "gesture":
			key_name = "key"
			self._validate_keys(body, {key_name})
			key = safe_text(body.get(key_name), 128)
			if not key:
				raise ValidationError("A non-empty 'key' value is required")
			self._validate_gesture(key)
			return self._main_action(lambda: self.adapter.execute_gesture(key), {"ok": True, "key": key})
		if action_name in ("set-caret", "set-selection"):
			if self.text_adapter is None:
				from .errors import ServiceUnavailable

				raise ServiceUnavailable("Text support is unavailable")
			object_id = body.get("objectId")
			generation = body.get("generation")
			if not isinstance(object_id, str) or not object_id:
				raise ValidationError("An 'objectId' value is required")
			if not isinstance(generation, str) or not generation:
				raise ValidationError("A 'generation' value is required")

			def text_action():
				obj, resolved_generation = self.registry.resolve(object_id, generation)
				self.adapter.assert_safe(obj)
				if action_name == "set-caret":
					return self.text_adapter.set_caret(obj, object_id, resolved_generation, body)
				return self.text_adapter.set_selection(obj, object_id, resolved_generation, body)

			return self.executor.call(text_action, 3000)
		if action_name in ("focus", "default-action"):
			self._validate_keys(body, {"objectId"}, optional={"generation"})
			object_id = body.get("objectId")
			if not object_id:
				raise ValidationError("An 'objectId' value is required")
			generation = body.get("generation")

			def object_action():
				obj, resolved_generation = self.registry.resolve(object_id, generation)
				self.adapter.assert_safe(obj)
				if action_name == "focus":
					self.adapter.focus_object(obj)
				else:
					self.adapter.default_action(obj)
				return resolved_generation

			resolved_generation = self.executor.call(object_action, 1000)
			return {"ok": True, "objectId": object_id, "generation": resolved_generation}
		raise ValidationError("Unknown action", details={"action": action_name})

	def _main_action(self, callback, response):
		def work():
			self.adapter.assert_safe()
			callback()
			return response

		return self.executor.call(work, 1000)

	@staticmethod
	def _validate_keys(body, required, optional=None):
		optional = optional or set()
		unknown = sorted(set(body) - required - optional)
		missing = sorted(key for key in required if key not in body)
		if unknown or missing:
			raise ValidationError("Invalid action fields", details={"unknown": unknown, "missing": missing})

	@staticmethod
	def _validate_gesture(key):
		normalized = key.strip().lower().replace(" ", "")
		if normalized.startswith("kb:"):
			normalized = normalized[3:]
		elif normalized.startswith("kb(") and "):" in normalized:
			normalized = normalized.split("):", 1)[1]
		aliases = {"ctrl": "control", "capslock": "nvda", "insert": "nvda", "numpadinsert": "nvda"}
		parts = {aliases.get(part, part) for part in normalized.split("+") if part}
		for blocked in BLOCKED_GESTURE_CHORDS:
			if set(blocked).issubset(parts):
				raise UnsafeAction("This gesture can reload or terminate NVDA")

	def capture_event(self, event_type, obj):
		if self._closing or self.security_state.restricted():
			return
		try:
			# Event callbacks are on NVDA's main thread. Registering an object uses
			# Python identity only and deliberately avoids UIA/IA2 property reads.
			object_id = self.registry.register(obj, self._event_generation)
			data = {"objectId": object_id, "generation": self._event_generation}
			self.events.append(event_type, data)
		except Exception:
			self.events.append(event_type, {"unavailable": True})

	def security_changed(self, locked=None, secure_desktop=None, unknown=None):
		_before, restricted = self.security_state.update(locked, secure_desktop, unknown)
		if restricted:
			self.clear_sensitive()
		else:
			self.events.set_enabled(True)
			self.speech_observer.set_enabled(True)

	def clear_sensitive(self):
		# Suspend buffers before clearing so callbacks racing with this transition
		# cannot repopulate them. Rotating the instance ID forces reconnecting SSE
		# clients to discard cursors from the pre-lock session.
		self.events.set_enabled(False, clear=True, reset_instance=True)
		self.speech_observer.set_enabled(False, clear=True)
		self.registry.clear()
		self._event_generation = self.registry.new_generation()
		self.exports.cancel_sensitive()
		if self.backups is not None:
			self.backups.cancel_sensitive()
		if self.diagnostic_exports is not None:
			self.diagnostic_exports.cancel_sensitive()

	def begin_close(self):
		if self._closing:
			return
		self._closing = True
		self.executor.close()
		self.exports.cancel_sensitive()
		if self.backups is not None:
			self.backups.cancel_sensitive()
		if self.diagnostic_exports is not None:
			self.diagnostic_exports.cancel_sensitive()
		self.speech_observer.set_enabled(False, clear=True)
		self.events.close()

	def close(self):
		if self._closed:
			return
		self.begin_close()
		self.exports.close()
		if self.backups is not None:
			self.backups.close()
		if self.diagnostic_exports is not None:
			self.diagnostic_exports.close()
		self.registry.clear()
		self.speech_observer.set_enabled(False, clear=True)
		self.events.close()
		self._closed = True
