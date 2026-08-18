import sys
import types
import unittest
from unittest.mock import patch

from support import GLOBAL_PLUGINS  # noqa: F401

from _nvdaHttpBridge.errors import GestureNotBound, UnsafeAction
from _nvdaHttpBridge.nvda_adapter import NvdaAdapter, _keyboard_gesture_name


class FakeSecurityState:
	def restricted(self):
		return False


class FakeGesture:
	def __init__(self, script):
		self.script = script


class FakeKeyboardInputGesture:
	gesture = None
	key = None

	@classmethod
	def fromName(cls, key):
		cls.key = key
		return cls.gesture


class NoInputGestureAction(LookupError):
	pass


class FakeInputManager:
	def __init__(self, error=None):
		self.error = error
		self.gestures = []

	def executeGesture(self, gesture):
		self.gestures.append(gesture)
		if self.error is not None:
			raise self.error


class NvdaAdapterGestureTests(unittest.TestCase):
	def setUp(self):
		self.adapter = NvdaAdapter(FakeSecurityState())

	def modules(self, script, error=None):
		gesture = FakeGesture(script)
		FakeKeyboardInputGesture.gesture = gesture
		manager = FakeInputManager(error)
		input_core = types.SimpleNamespace(
			manager=manager,
			NoInputGestureAction=NoInputGestureAction,
		)
		keyboard_handler = types.SimpleNamespace(KeyboardInputGesture=FakeKeyboardInputGesture)
		return gesture, manager, {
			"inputCore": input_core,
			"keyboardHandler": keyboard_handler,
		}

	def test_bound_current_context_gesture_uses_nvda_dispatch(self):
		def script_appModule(gesture):
			pass

		gesture, manager, modules = self.modules(script_appModule)
		with patch.dict(sys.modules, modules):
			self.adapter.execute_gesture("NVDA+control+upArrow")

		self.assertEqual("NVDA+control+upArrow", FakeKeyboardInputGesture.key)
		self.assertEqual([gesture], manager.gestures)

	def test_normalized_identifier_moves_the_main_key_after_modifiers(self):
		cases = {
			"kb:control+downarrow+nvda": "control+nvda+downarrow",
			"kb:nvda+r+shift": "nvda+shift+r",
			"kb(laptop):control+numpaddelete+windows": "control+windows+numpaddelete",
			"NVDA+control+upArrow": "NVDA+control+upArrow",
		}
		for identifier, expected in cases.items():
			with self.subTest(identifier=identifier):
				self.assertEqual(expected, _keyboard_gesture_name(identifier))

	def test_normalized_current_context_gesture_uses_reordered_name(self):
		def script_globalPlugin(gesture):
			pass

		gesture, manager, modules = self.modules(script_globalPlugin)
		with patch.dict(sys.modules, modules):
			self.adapter.execute_gesture("kb:nvda+r+shift")

		self.assertEqual("nvda+shift+r", FakeKeyboardInputGesture.key)
		self.assertEqual([gesture], manager.gestures)

	def test_unbound_current_context_gesture_is_a_structured_conflict(self):
		gesture, manager, modules = self.modules(None, NoInputGestureAction())
		with patch.dict(sys.modules, modules), self.assertRaises(GestureNotBound) as caught:
			self.adapter.execute_gesture("NVDA+control+upArrow")

		self.assertEqual([gesture], manager.gestures)
		self.assertEqual(409, caught.exception.status)
		self.assertEqual("gestureNotBound", caught.exception.code)
		self.assertEqual({"key": "NVDA+control+upArrow"}, caught.exception.details)

	def test_lifecycle_gesture_is_rejected_before_dispatch(self):
		def script_restart(gesture):
			pass

		_gesture, manager, modules = self.modules(script_restart)
		with patch.dict(sys.modules, modules), self.assertRaises(UnsafeAction):
			self.adapter.execute_gesture("NVDA+shift+q")

		self.assertEqual([], manager.gestures)


if __name__ == "__main__":
	unittest.main()
