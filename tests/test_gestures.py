import copy
import unittest

from support import GLOBAL_PLUGINS  # noqa: F401

from _nvdaHttpBridge.errors import StaleState, ValidationError
from _nvdaHttpBridge.gestures import GesturesAdapter


class Info:
	moduleName = "globalCommands"
	className = "GlobalCommands"
	scriptName = "sayTime"
	gestures = ["kb:nvda+f12"]


class FakeBackend:
	def __init__(self):
		self.user = []
		self.context = {"focus": "one"}
		self.save_calls = 0
		self.writable = True

	def snapshot(self):
		return {"Misc": {"Report time": Info()}}, dict(self.context), copy.deepcopy(self.user)

	def normalize(self, identifier):
		if not isinstance(identifier, str) or ":" not in identifier:
			raise ValueError("bad gesture")
		prefix, keys = identifier.lower().split(":", 1)
		return prefix + ":" + "+".join(sorted(keys.split("+")))

	def add(self, gesture, module, class_name, script):
		self.user.append((gesture, module, class_name, script))

	def remove(self, gesture, module, class_name, script):
		self.user.remove((gesture, module, class_name, script))

	def save(self):
		self.save_calls += 1

	def capture_map(self):
		return copy.deepcopy(self.user)

	def restore_map(self, saved):
		self.user = copy.deepcopy(saved)

	def should_write(self):
		return self.writable


class GesturesAdapterTests(unittest.TestCase):
	def test_add_and_unbind_save_once(self):
		backend = FakeBackend()
		adapter = GesturesAdapter(backend)
		before = adapter.get()
		target = {"module": "globalCommands", "class": "GlobalCommands", "script": "sayTime"}
		result = adapter.patch({
			"baseRevision": before["revision"],
			"operations": [
				{"action": "add", "gesture": "kb:NVDA+t", "target": target},
				{"action": "unbind", "gesture": "kb:NVDA+f12", "target": target},
			],
		})
		self.assertTrue(result["changed"])
		self.assertEqual(1, backend.save_calls)
		self.assertIn(("kb:f12+nvda", "globalCommands", "GlobalCommands", None), backend.user)

	def test_context_change_is_stale_and_reset_is_rejected(self):
		backend = FakeBackend()
		adapter = GesturesAdapter(backend)
		before = adapter.get()
		backend.context["focus"] = "two"
		with self.assertRaises(StaleState):
			adapter.patch({"baseRevision": before["revision"], "operations": [{"action": "reset"}]})
		fresh = adapter.get()
		with self.assertRaises(ValidationError):
			adapter.patch({"baseRevision": fresh["revision"], "operations": [{"action": "reset"}]})

	def test_disk_disabled_keeps_effective_mapping_without_saving(self):
		backend = FakeBackend()
		backend.writable = False
		adapter = GesturesAdapter(backend)
		before = adapter.get()
		target = {"module": "globalCommands", "class": "GlobalCommands", "script": "sayTime"}
		result = adapter.patch({
			"baseRevision": before["revision"],
			"operations": [{"action": "add", "gesture": "kb:nvda+t", "target": target}],
		})
		self.assertEqual(0, backend.save_calls)
		self.assertFalse(result["persisted"])


if __name__ == "__main__":
	unittest.main()
