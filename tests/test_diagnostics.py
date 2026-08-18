import json
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch
import zipfile

from support import GLOBAL_PLUGINS  # noqa: F401

from _nvdaHttpBridge.diagnostics import DiagnosticsAdapter, DiagnosticsExportManager, NvdaDiagnosticsBackend
from _nvdaHttpBridge.errors import SecureContext


class DirectExecutor:
	def call(self, callback, timeout_ms):
		return callback()


class FakeBackend:
	def addons(self):
		return [{"name": "addon", "running": True}]

	def global_plugins(self):
		return [{"module": "globalPlugins.example", "class": "GlobalPlugin"}]

	def drivers(self):
		return {"synthesizers": {"active": "synth"}, "brailleDisplays": {"active": "display"}}

	def snapshot(self):
		return {
			"addons": self.addons(),
			"globalPlugins": self.global_plugins(),
			"drivers": self.drivers(),
		}


class DiagnosticsTests(unittest.TestCase):
	def test_driver_inventory_uses_nvda_2026_braille_module_api(self):
		braille = types.ModuleType("braille")
		braille.handler = types.SimpleNamespace(display=types.SimpleNamespace(name="display"))
		braille.getDisplayList = lambda: [("display", "Display")]
		synth_driver_handler = types.ModuleType("synthDriverHandler")
		synth_driver_handler.getSynth = lambda: types.SimpleNamespace(name="synth")
		synth_driver_handler.getSynthList = lambda: [("synth", "Synth")]

		with patch.dict(sys.modules, {"braille": braille, "synthDriverHandler": synth_driver_handler}):
			result = NvdaDiagnosticsBackend().drivers()

		self.assertEqual("synth", result["synthesizers"]["active"])
		self.assertEqual([{"id": "display", "displayName": "Display"}], result["brailleDisplays"]["available"])

	def test_inventory_shapes_are_explicit(self):
		adapter = DiagnosticsAdapter(FakeBackend())
		self.assertEqual(1, adapter.addons()["count"])
		self.assertEqual(1, adapter.global_plugins()["count"])
		self.assertEqual("synth", adapter.drivers()["synthesizers"]["active"])

	def test_export_creates_bounded_zip_with_json_and_log_tail(self):
		with tempfile.TemporaryDirectory() as temp:
			log_path = temp + "\\nvda.log"
			with open(log_path, "wb") as log_file:
				log_file.write(b"log-tail")
			manager = DiagnosticsExportManager(
				DirectExecutor(), DiagnosticsAdapter(FakeBackend()), lambda: None, temp,
				log_path_provider=lambda: log_path, reaper_interval=100,
			)
			try:
				created = manager.create()
				for _unused in range(100):
					status = manager.status(created["jobId"])
					if status["status"] not in ("queued", "running"):
						break
					time.sleep(0.01)
				self.assertEqual("completed", status["status"])
				data_file, _length = manager.open_data(created["jobId"])
				with data_file, zipfile.ZipFile(data_file) as archive:
					self.assertEqual(
						{"diagnostics.json", "nvda-log-tail.txt"}, set(archive.namelist()),
					)
					payload = json.loads(archive.read("diagnostics.json").decode("utf-8"))
					self.assertEqual("addon", payload["addons"][0]["name"])
					self.assertEqual(b"log-tail", archive.read("nvda-log-tail.txt"))
			finally:
				manager.close()

	def test_restricted_context_rejects_creation(self):
		with tempfile.TemporaryDirectory() as temp:
			manager = DiagnosticsExportManager(
				DirectExecutor(), DiagnosticsAdapter(FakeBackend()),
				lambda: (_ for _ in ()).throw(SecureContext()), temp,
				reaper_interval=100,
			)
			try:
				with self.assertRaises(SecureContext):
					manager.create()
			finally:
				manager.close()


if __name__ == "__main__":
	unittest.main()
