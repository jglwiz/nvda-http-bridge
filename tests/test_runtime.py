import importlib.util
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import patch

from support import GLOBAL_PLUGINS

from _nvdaHttpBridge.runtime import BridgeRuntime


class FakeExtensionPoint:
	def __init__(self, name):
		self.name = name
		self.registered = []
		self.unregistered = []

	def register(self, callback):
		self.registered.append(callback)

	def unregister(self, callback):
		self.unregistered.append(callback)


class FakeLogger:
	def __init__(self):
		self.warnings = []

	def info(self, *args, **kwargs):
		pass

	def exception(self, *args, **kwargs):
		pass

	def debugWarning(self, *args, **kwargs):
		self.warnings.append((args, kwargs))


class CloseCounter:
	def __init__(self):
		self.close_calls = 0
		self.stop_calls = 0

	def close(self):
		self.close_calls += 1

	def stop(self):
		self.stop_calls += 1


class RuntimeExtensionTests(unittest.TestCase):
	def extension_modules(self, speech, lock, secure):
		speech_package = types.ModuleType("speech")
		speech_package.__path__ = []
		speech_extensions = types.ModuleType("speech.extensions")
		speech_extensions.pre_speech = speech
		speech_package.extensions = speech_extensions

		utils_package = types.ModuleType("utils")
		utils_package.__path__ = []
		security_module = types.ModuleType("utils.security")
		security_module.post_sessionLockStateChanged = lock
		utils_package.security = security_module

		win_package = types.ModuleType("winAPI")
		win_package.__path__ = []
		secure_module = types.ModuleType("winAPI.secureDesktop")
		secure_module.post_secureDesktopStateChange = secure
		win_package.secureDesktop = secure_module

		return {
			"speech": speech_package,
			"speech.extensions": speech_extensions,
			"utils": utils_package,
			"utils.security": security_module,
			"winAPI": win_package,
			"winAPI.secureDesktop": secure_module,
		}

	def test_extensions_register_and_unregister_once_with_same_callbacks(self):
		extensions = [FakeExtensionPoint(name) for name in ("speech", "lock", "secure")]
		service = CloseCounter()
		server = CloseCounter()
		runtime = BridgeRuntime.__new__(BridgeRuntime)
		runtime.log = FakeLogger()
		runtime.service = service
		runtime.server = server
		runtime._registered = []
		runtime._lock = threading.RLock()
		runtime._started = True
		runtime._closing = False
		runtime._speech_callback = lambda *args, **kwargs: None
		runtime._lock_callback = lambda *args, **kwargs: None
		runtime._secure_callback = lambda *args, **kwargs: None

		with patch.dict(sys.modules, self.extension_modules(*extensions)):
			runtime._register_extensions()

		self.assertEqual([runtime._speech_callback], extensions[0].registered)
		self.assertEqual([runtime._lock_callback], extensions[1].registered)
		self.assertEqual([runtime._secure_callback], extensions[2].registered)

		runtime.close()
		runtime.close()

		for extension in extensions:
			self.assertEqual(extension.registered, extension.unregistered)
		self.assertEqual(1, service.close_calls)
		self.assertEqual(1, server.stop_calls)


class EntryEventTests(unittest.TestCase):
	def load_entry_module(self, logger):
		global_plugin_handler = types.ModuleType("globalPluginHandler")

		class BaseGlobalPlugin:
			def terminate(self):
				pass

		global_plugin_handler.GlobalPlugin = BaseGlobalPlugin
		log_handler = types.ModuleType("logHandler")
		log_handler.log = logger

		package_name = "_nvda_http_bridge_entry_test"
		package = types.ModuleType(package_name)
		package.__path__ = [str(GLOBAL_PLUGINS)]
		module_name = package_name + ".nvdaHttpBridge"
		entry_path = Path(GLOBAL_PLUGINS) / "nvdaHttpBridge.py"
		spec = importlib.util.spec_from_file_location(module_name, entry_path)
		module = importlib.util.module_from_spec(spec)
		aliases = {
			"globalPluginHandler": global_plugin_handler,
			"logHandler": log_handler,
			package_name: package,
			package_name + "._nvdaHttpBridge": sys.modules["_nvdaHttpBridge"],
			package_name + "._nvdaHttpBridge.runtime": sys.modules["_nvdaHttpBridge.runtime"],
			module_name: module,
		}
		with patch.dict(sys.modules, aliases):
			spec.loader.exec_module(module)
		return module

	def test_capture_failure_still_calls_next_handler_exactly_once(self):
		logger = FakeLogger()
		entry = self.load_entry_module(logger)

		class FailingRuntime:
			def __init__(self):
				self.capture_calls = 0

			def capture_event(self, event_name, obj):
				self.capture_calls += 1
				raise RuntimeError("capture failed")

		runtime = FailingRuntime()
		plugin = entry.GlobalPlugin.__new__(entry.GlobalPlugin)
		plugin._runtime = runtime
		next_calls = []

		plugin.event_gainFocus(object(), lambda: next_calls.append("next"))

		self.assertEqual(["next"], next_calls)
		self.assertEqual(1, runtime.capture_calls)
		self.assertEqual(1, len(logger.warnings))


if __name__ == "__main__":
	unittest.main()
