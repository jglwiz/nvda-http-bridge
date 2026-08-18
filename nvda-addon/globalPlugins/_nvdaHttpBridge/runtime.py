"""Single-owner lifecycle for the NVDA HTTP bridge."""

import threading

from .auth import SecurityState
from .backups import BackupManager
from .diagnostics import DiagnosticsAdapter, DiagnosticsExportManager
from .events import EventBuffer, SpeechObserver
from .executor import MainThreadExecutor
from .exports import ExportManager
from .gestures import GesturesAdapter
from .nvda_adapter import NvdaAdapter
from .settings import SettingsAdapter
from .status import StatusAdapter
from .speech_dictionaries import SpeechDictionariesAdapter
from .symbol_dictionaries import SymbolDictionariesAdapter
from .text import TextAdapter
from .serialization import ObjectRegistry
from .server import BoundedHTTPServer
from .service import BridgeService


class BridgeRuntime:
	def __init__(self, logger):
		self.log = logger
		self.security = SecurityState(unknown=True)
		self.adapter = NvdaAdapter(self.security)
		initial = self.adapter.initial_security_state()
		self.security.update(**initial)
		capture_enabled = not self.security.restricted()
		self.events = EventBuffer(enabled=capture_enabled)
		self.speech = SpeechObserver(
			self.events,
			capture_allowed=lambda: not self.security.restricted(),
		)
		self.speech.set_enabled(capture_enabled)
		self.registry = ObjectRegistry(adapter=self.adapter)
		self.executor = MainThreadExecutor(self.adapter.schedule)
		self.settings = SettingsAdapter()
		self.status = StatusAdapter()
		self.text = TextAdapter()
		self.diagnostics = DiagnosticsAdapter()
		self.speech_dictionaries = SpeechDictionariesAdapter()
		self.symbol_dictionaries = SymbolDictionariesAdapter()
		self.gestures = GesturesAdapter()
		self.exports = ExportManager(
			self.executor,
			self.adapter.get_root,
			self.registry,
			self.adapter.assert_safe,
			self.adapter.temp_path,
			adapter=self.adapter,
			defer_start=True,
		)
		self.backups = BackupManager(
			self.adapter,
			self.adapter.assert_safe,
			defer_start=True,
		)
		self.diagnostic_exports = DiagnosticsExportManager(
			self.executor,
			self.diagnostics,
			self.adapter.assert_safe,
			self.adapter.temp_path,
			log_path_provider=lambda: self.adapter.log_path,
			defer_start=True,
		)
		self.service = BridgeService(
			self.adapter,
			self.executor,
			self.registry,
			self.events,
			self.speech,
			self.exports,
			self.security,
			backups=self.backups,
			settings=self.settings,
			status=self.status,
			text=self.text,
			diagnostics=self.diagnostics,
			diagnostic_exports=self.diagnostic_exports,
			speech_dictionaries=self.speech_dictionaries,
			symbol_dictionaries=self.symbol_dictionaries,
			gestures=self.gestures,
			logger=self.log,
		)
		self.server = None
		self._registered = []
		self._lock = threading.RLock()
		self._started = False
		self._closing = False
		# Keep strong references: NVDA extension points store weak references.
		self._speech_callback = self.speech.on_pre_speech
		self._lock_callback = self._on_lock_state
		self._secure_callback = self._on_secure_desktop

	def start(self):
		with self._lock:
			if self._started:
				return
			if self._closing:
				raise RuntimeError("Bridge runtime is closing")
		try:
			self.server = BoundedHTTPServer(self.service, logger=self.log)
			self.exports.start()
			self.backups.start()
			self.diagnostic_exports.start()
			self._register_extensions()
			self.server.start()
			with self._lock:
				self._started = True
			self.log.info("nvdaHttpBridge: started on 127.0.0.1:%s", self.server.server_address[1])
		except Exception:
			self.close()
			raise

	def _register_extensions(self):
		from speech.extensions import pre_speech
		from utils.security import post_sessionLockStateChanged
		from winAPI.secureDesktop import post_secureDesktopStateChange

		for extension, callback in (
			(pre_speech, self._speech_callback),
			(post_sessionLockStateChanged, self._lock_callback),
			(post_secureDesktopStateChange, self._secure_callback),
		):
			extension.register(callback)
			self._registered.append((extension, callback))

	def _on_lock_state(self, isNowLocked=False, **kwargs):
		self.service.security_changed(locked=bool(isNowLocked), unknown=False)

	def _on_secure_desktop(self, isSecureDesktop=False, **kwargs):
		self.service.security_changed(secure_desktop=bool(isSecureDesktop), unknown=False)

	def capture_event(self, event_type, obj):
		if self._closing:
			return
		self.service.capture_event(event_type, obj)

	def close(self):
		with self._lock:
			if self._closing:
				return
			self._closing = True
		for extension, callback in reversed(self._registered):
			try:
				extension.unregister(callback)
			except Exception:
				self.log.debugWarning("nvdaHttpBridge: failed to unregister an extension point", exc_info=True)
		self._registered.clear()
		if self.server is not None:
			begin_server_close = getattr(self.server, "begin_close", None)
			if begin_server_close is not None:
				begin_server_close()
		try:
			begin_service_close = getattr(self.service, "begin_close", None)
			if begin_service_close is not None:
				begin_service_close()
		except Exception:
			self.log.exception("nvdaHttpBridge: failed while beginning bridge shutdown")
		if self.server is not None:
			try:
				self.server.stop()
			except Exception:
				self.log.exception("nvdaHttpBridge: failed while stopping HTTP server")
			self.server = None
		try:
			self.service.close()
		except Exception:
				self.log.exception("nvdaHttpBridge: failed while closing bridge services")
		self.log.info("nvdaHttpBridge: stopped")
