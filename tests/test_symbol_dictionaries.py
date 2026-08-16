import unittest

from support import GLOBAL_PLUGINS  # noqa: F401

from _nvdaHttpBridge.errors import ValidationError
from _nvdaHttpBridge.symbol_dictionaries import SymbolDictionariesAdapter


class Symbol:
	def __init__(self, identifier, replacement, level=300, preserve=0, display_name=None):
		self.identifier = identifier
		self.pattern = None
		self.replacement = replacement
		self.level = level
		self.preserve = preserve
		self.displayName = display_name or identifier


class UserSymbols:
	def __init__(self):
		self.symbols = {}
		self.save_calls = 0

	def save(self):
		self.save_calls += 1


class Processor:
	def __init__(self):
		self.locale = "en"
		self.computedSymbols = {"!": Symbol("!", "bang")}
		self.userSymbols = UserSymbols()

	def isBuiltin(self, identifier):
		return identifier == "!"

	def updateSymbol(self, symbol):
		self.userSymbols.symbols[symbol.identifier] = symbol
		self.computedSymbols[symbol.identifier] = symbol
		return True

	def deleteSymbol(self, symbol):
		self.userSymbols.symbols.pop(symbol.identifier, None)
		self.computedSymbols.pop(symbol.identifier, None)


class FakeBackend:
	def __init__(self):
		self.value = Processor()
		self.invalidated = []
		self.writable = True

	def current_locale(self):
		return "en"

	def available_locales(self):
		return ["en"]

	def processor(self, locale, fallback=False):
		return self.value

	def new_symbol(self, identifier, replacement, level, preserve, display_name):
		return Symbol(identifier, replacement, level, preserve, display_name)

	def invalidate(self, locale):
		self.invalidated.append(locale)

	def should_write(self):
		return self.writable


class SymbolDictionaryAdapterTests(unittest.TestCase):
	def test_add_symbol_saves_and_invalidates_locale(self):
		backend = FakeBackend()
		adapter = SymbolDictionariesAdapter(backend)
		before = adapter.get("en")
		result = adapter.put("en", {
			"baseRevision": before["revision"],
			"updates": [{"identifier": "@", "replacement": "at", "level": "most", "preserve": "always"}],
		})
		self.assertTrue(result["changed"])
		self.assertEqual(1, backend.value.userSymbols.save_calls)
		self.assertEqual(["en"], backend.invalidated)

	def test_pure_builtin_cannot_be_removed(self):
		adapter = SymbolDictionariesAdapter(FakeBackend())
		before = adapter.get("en")
		with self.assertRaises(ValidationError):
			adapter.put("en", {"baseRevision": before["revision"], "remove": ["!"]})

	def test_symbol_changes_remain_in_memory_without_writing_when_disk_is_disabled(self):
		backend = FakeBackend()
		backend.writable = False
		adapter = SymbolDictionariesAdapter(backend)
		before = adapter.get("en")
		result = adapter.put("en", {
			"baseRevision": before["revision"],
			"updates": [{"identifier": "@", "replacement": "at"}],
		})
		self.assertEqual(0, backend.value.userSymbols.save_calls)
		self.assertFalse(result["persisted"])


if __name__ == "__main__":
	unittest.main()
