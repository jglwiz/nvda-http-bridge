import sys
import types
import unittest
from unittest.mock import patch

from support import GLOBAL_PLUGINS  # noqa: F401

from _nvdaHttpBridge.errors import StaleState, ValidationError
from _nvdaHttpBridge.text import NvdaTextBackend, TextAdapter


class FakeInfo:
	def __init__(self, text, collapsed=False):
		self.text = text
		self.isCollapsed = collapsed


class FakeBackend:
	def __init__(self):
		self.text = "abcdefghij"
		self.signature_value = "v1"
		self.signature_known_positions = None
		self.writes = []

	def current_info(self, obj, position):
		return FakeInfo("" if position == "caret" else "def", collapsed=position == "caret")

	def object_chunk(self, obj, offset, max_chars):
		if offset > len(self.text):
			raise ValidationError()
		chunk = self.text[offset:offset + max_chars]
		return chunk, len(chunk), offset + len(chunk) < len(self.text)

	def signature(self, obj, known_positions=None):
		self.signature_known_positions = known_positions
		return self.signature_value

	def set_range(self, obj, start, end, caret=False):
		self.writes.append((start, end, caret))
		self.signature_value = "v2"


class TextAdapterTests(unittest.TestCase):
	def setUp(self):
		self.backend = FakeBackend()
		self.adapter = TextAdapter(self.backend)

	def test_object_text_is_bounded_and_pageable(self):
		result = self.adapter.object_text(object(), "obj", "gen", 2, 4)
		self.assertEqual("cdef", result["text"])
		self.assertEqual(6, result["nextOffset"])
		self.assertTrue(result["truncated"])

	def test_current_range_returns_opaque_revision_without_native_bookmark(self):
		result = self.adapter.current("selection", object(), "obj", "gen", 10)
		self.assertEqual("def", result["text"])
		self.assertNotIn("bookmark", result)
		self.assertFalse(result["collapsed"])
		self.assertEqual("def", self.backend.signature_known_positions["selection"].text)

	def test_set_caret_and_selection_require_fresh_revision_and_bounded_offsets(self):
		obj = object()
		before = self.adapter.object_text(obj, "obj", "gen", 0, 10)
		result = self.adapter.set_caret(obj, "obj", "gen", {
			"objectId": "obj", "generation": "gen",
			"baseRevision": before["revision"], "offset": 3,
		})
		self.assertEqual(3, result["offset"])
		self.assertEqual([(3, 3, True)], self.backend.writes)
		with self.assertRaises(StaleState):
			self.adapter.set_selection(obj, "obj", "gen", {
				"objectId": "obj", "generation": "gen",
				"baseRevision": before["revision"], "start": 1, "end": 2,
			})
		fresh = self.adapter.object_text(obj, "obj", "gen", 0, 10)
		with self.assertRaises(ValidationError):
			self.adapter.set_selection(obj, "obj", "gen", {
				"objectId": "obj", "generation": "gen",
				"baseRevision": fresh["revision"], "start": 5, "end": 4,
			})

	def test_query_window_rejects_unknown_repeated_and_excessive_values(self):
		self.assertEqual((0, 4096), self.adapter.parse_window({}))
		for params in ({"other": ["1"]}, {"offset": ["1", "2"]}, {"maxChars": ["999999"]}):
			with self.subTest(params=params), self.assertRaises(ValidationError):
				self.adapter.parse_window(params)


class NvdaTextBackendTests(unittest.TestCase):
	def test_current_selection_fallback_is_reused_without_second_provider_query(self):
		text_infos = types.ModuleType("textInfos")
		text_infos.POSITION_CARET = "caret"
		text_infos.POSITION_SELECTION = "selection"

		class Backend(NvdaTextBackend):
			def object_chunk(self, obj, offset, max_chars):
				return "", 0, False

		class Object:
			def __init__(self):
				self.calls = []

			def makeTextInfo(self, position):
				self.calls.append(position)
				if position == "selection":
					raise RuntimeError("No selection available")
				return FakeInfo("", collapsed=True)

		obj = Object()
		with patch.dict(sys.modules, {"textInfos": text_infos}):
			result = TextAdapter(Backend()).current("selection", obj, "obj", "gen", 10)

		self.assertEqual("", result["text"])
		self.assertTrue(result["collapsed"])
		self.assertTrue(result["revision"])
		self.assertEqual(1, obj.calls.count("selection"))
		self.assertEqual(2, obj.calls.count("caret"))

	def test_unavailable_selection_falls_back_to_collapsed_caret(self):
		text_infos = types.ModuleType("textInfos")
		text_infos.POSITION_CARET = "caret"
		text_infos.POSITION_SELECTION = "selection"

		class Object:
			def __init__(self):
				self.calls = []

			def makeTextInfo(self, position):
				self.calls.append(position)
				if position == "selection":
					raise RuntimeError("No selection available")
				return FakeInfo("", collapsed=True)

		obj = Object()
		with patch.dict(sys.modules, {"textInfos": text_infos}):
			info = NvdaTextBackend().current_info(obj, "selection")

		self.assertTrue(info.isCollapsed)
		self.assertEqual(["selection", "caret"], obj.calls)

	def test_caret_provider_errors_are_propagated_without_fallback(self):
		text_infos = types.ModuleType("textInfos")
		text_infos.POSITION_CARET = "caret"
		text_infos.POSITION_SELECTION = "selection"

		class Object:
			def __init__(self, error):
				self.error = error
				self.calls = []

			def makeTextInfo(self, position):
				self.calls.append(position)
				raise self.error

		with patch.dict(sys.modules, {"textInfos": text_infos}):
			for error in (RuntimeError("caret failed"), NotImplementedError("caret unsupported")):
				with self.subTest(error=type(error).__name__):
					obj = Object(error)
					with self.assertRaises(type(error)):
						NvdaTextBackend().current_info(obj, "caret")
					self.assertEqual(["caret"], obj.calls)


if __name__ == "__main__":
	unittest.main()
