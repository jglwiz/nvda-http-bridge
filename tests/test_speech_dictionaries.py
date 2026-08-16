import unittest

from support import GLOBAL_PLUGINS  # noqa: F401

from _nvdaHttpBridge.errors import ValidationError
from _nvdaHttpBridge.speech_dictionaries import SpeechDictionariesAdapter


class FakeEntry:
	def __init__(self, raw):
		self.pattern = raw["pattern"]
		self.replacement = raw["replacement"]
		self.comment = raw["comment"]
		self.caseSensitive = raw["caseSensitive"]
		self.type = {"anywhere": 0, "regexp": 1, "word": 2}[raw["type"]]


class FakeDictionary(list):
	def __init__(self, *args):
		super().__init__(*args)
		self.save_calls = 0

	def save(self):
		self.save_calls += 1


class FakeBackend:
	def __init__(self):
		self.writable = True
		self.dictionaries = {
			"default": FakeDictionary([FakeEntry({
				"pattern": "NVDA", "replacement": "nvda", "comment": "", "caseSensitive": True, "type": "anywhere",
			})]),
			"voice": FakeDictionary(),
			"temp": FakeDictionary(),
		}

	def resolve(self, dictionary_id):
		return self.dictionaries[dictionary_id], dictionary_id, None, FakeEntry

	def supported_types(self, dictionary_id):
		return {"anywhere": 0, "regexp": 1, "word": 2}

	def make_entry(self, dictionary_id, raw):
		if raw["pattern"] == "[":
			raise ValueError("invalid regular expression")
		return FakeEntry(raw)

	def should_write(self):
		return self.writable


class SpeechDictionaryAdapterTests(unittest.TestCase):
	def test_put_uses_dictionary_save_and_temp_is_not_persistent(self):
		backend = FakeBackend()
		adapter = SpeechDictionariesAdapter(backend)
		before = adapter.get("temp")
		entry = {"pattern": "x", "replacement": "y", "comment": "", "caseSensitive": False, "type": "word"}
		result = adapter.put("temp", {"baseRevision": before["revision"], "entries": [entry]})
		self.assertEqual(1, backend.dictionaries["temp"].save_calls)
		self.assertFalse(result["persisted"])
		self.assertEqual([entry], result["state"]["entries"])

	def test_invalid_entry_and_clear_all_are_rejected_before_mutation(self):
		backend = FakeBackend()
		adapter = SpeechDictionariesAdapter(backend)
		before = adapter.get("default")
		with self.assertRaises(ValidationError):
			adapter.validate("default", {"entries": [{"pattern": "[", "replacement": "", "type": "regexp"}]})
		with self.assertRaises(ValidationError):
			adapter.put("default", {"baseRevision": before["revision"], "entries": []})
		self.assertEqual(1, len(backend.dictionaries["default"]))

	def test_persistent_dictionary_does_not_write_when_nvda_disallows_disk_writes(self):
		backend = FakeBackend()
		backend.writable = False
		adapter = SpeechDictionariesAdapter(backend)
		before = adapter.get("default")
		entry = {"pattern": "new", "replacement": "value", "comment": "", "caseSensitive": True, "type": "anywhere"}
		result = adapter.put("default", {"baseRevision": before["revision"], "entries": [entry]})
		self.assertEqual(0, backend.dictionaries["default"].save_calls)
		self.assertFalse(result["persisted"])


if __name__ == "__main__":
	unittest.main()
