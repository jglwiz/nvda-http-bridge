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
			self._close_runtime()

	def terminate(self):
		try:
			self._close_runtime()
		finally:
			super().terminate()

	def _close_runtime(self):
		runtime = self._runtime
		self._runtime = None
		if runtime is None:
			return
		try:
			runtime.close()
		except Exception:
			log.exception("nvdaHttpBridge: runtime cleanup failed")

	def _event(self, event_name, obj, nextHandler):
		try:
			return nextHandler()
		finally:
			runtime = self._runtime
			if runtime is not None:
				try:
					runtime.capture_event(event_name, obj)
				except Exception:
					log.debugWarning("nvdaHttpBridge: unable to capture %s event" % event_name, exc_info=True)

	def event_gainFocus(self, obj, nextHandler, **kwargs):
		return self._event("gainFocus", obj, nextHandler)

	def event_foreground(self, obj, nextHandler, **kwargs):
		return self._event("foreground", obj, nextHandler)

	def event_nameChange(self, obj, nextHandler, **kwargs):
		return self._event("nameChange", obj, nextHandler)

	def event_valueChange(self, obj, nextHandler, **kwargs):
		return self._event("valueChange", obj, nextHandler)

	def event_stateChange(self, obj, nextHandler, **kwargs):
		return self._event("stateChange", obj, nextHandler)

	def event_caret(self, obj, nextHandler, **kwargs):
		return self._event("caret", obj, nextHandler)
