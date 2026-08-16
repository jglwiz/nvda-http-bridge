"""NVDA global-plugin entry point for NVDA HTTP Bridge."""

import globalPluginHandler
from logHandler import log

from ._nvdaHttpBridge.runtime import BridgeRuntime


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		super().__init__()
		self._runtime = None
		try:
			self._runtime = BridgeRuntime(log)
			self._runtime.start()
		except Exception:
			log.exception("nvdaHttpBridge: initialization failed; NVDA will continue without HTTP")
			if self._runtime is not None:
				self._runtime.close()
			self._runtime = None

	def terminate(self):
		runtime = self._runtime
		self._runtime = None
		if runtime is not None:
			runtime.close()
		super().terminate()

	def _event(self, event_name, obj, nextHandler):
		try:
			nextHandler()
		finally:
			runtime = self._runtime
			if runtime is not None:
				try:
					runtime.capture_event(event_name, obj)
				except Exception:
					log.debugWarning("nvdaHttpBridge: unable to capture %s event" % event_name, exc_info=True)

	def event_gainFocus(self, obj, nextHandler, **kwargs):
		self._event("gainFocus", obj, nextHandler)

	def event_foreground(self, obj, nextHandler, **kwargs):
		self._event("foreground", obj, nextHandler)

	def event_nameChange(self, obj, nextHandler, **kwargs):
		self._event("nameChange", obj, nextHandler)

	def event_valueChange(self, obj, nextHandler, **kwargs):
		self._event("valueChange", obj, nextHandler)

	def event_stateChange(self, obj, nextHandler, **kwargs):
		self._event("stateChange", obj, nextHandler)

	def event_caret(self, obj, nextHandler, **kwargs):
		self._event("caret", obj, nextHandler)
