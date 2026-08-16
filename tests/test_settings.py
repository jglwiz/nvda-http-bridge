import unittest

from support import GLOBAL_PLUGINS  # noqa: F401

from _nvdaHttpBridge.errors import Conflict, StaleState, ValidationError
from _nvdaHttpBridge.settings import SettingsAdapter


class FakeBackend:
	def __init__(self, forced=False):
		self.data = {
			"language": "Windows",
			"saveConfigurationOnExit": True,
			"askToExit": True,
			"playStartAndExitSounds": True,
			"preventDisplayTurningOff": True,
		}
		self.forced = forced

	def values(self):
		return dict(self.data)

	def languages(self):
		return [{"id": "Windows", "displayName": "User default"}, {"id": "zh_CN", "displayName": "Chinese"}]

	def language_forced(self):
		return self.forced

	def scope(self):
		return {"kind": "activeConfiguration", "profile": None}

	def write(self, values):
		self.data.update(values)


class SettingsAdapterTests(unittest.TestCase):
	def test_patch_validates_revision_and_returns_restart_without_persisting(self):
		adapter = SettingsAdapter(FakeBackend())
		before = adapter.get_general()
		result = adapter.patch_general({"baseRevision": before["revision"], "values": {"language": "zh_CN"}})
		self.assertTrue(result["changed"])
		self.assertTrue(result["restartRequired"])
		self.assertFalse(result["persisted"])
		self.assertEqual("zh_CN", result["state"]["values"]["language"])
		with self.assertRaises(StaleState):
			adapter.patch_general({"baseRevision": before["revision"], "values": {"askToExit": False}})

	def test_forced_language_and_unknown_fields_are_rejected(self):
		adapter = SettingsAdapter(FakeBackend(forced=True))
		before = adapter.get_general()
		with self.assertRaises(Conflict):
			adapter.patch_general({"baseRevision": before["revision"], "values": {"language": "zh_CN"}})
		with self.assertRaises(ValidationError):
			adapter.patch_general({"baseRevision": before["revision"], "values": {"arbitrary": True}})


if __name__ == "__main__":
	unittest.main()
