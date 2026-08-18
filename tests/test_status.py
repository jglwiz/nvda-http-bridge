import unittest

from support import GLOBAL_PLUGINS  # noqa: F401

from _nvdaHttpBridge.errors import Conflict, StaleState, ValidationError
from _nvdaHttpBridge.status import StatusAdapter


class FakeBackend:
	def __init__(self):
		self.state = {
			"activeProfile": "work",
			"application": {"name": "editor", "processId": 42},
			"speech": {"synthesizer": "oneCore", "voice": "voice"},
			"braille": {"display": "noBraille", "cells": 0},
			"modes": {
				"inputHelp": False,
				"sleepMode": False,
				"browseMode": True,
				"screenCurtain": False,
			},
			"modeAvailability": {
				"inputHelp": True,
				"sleepMode": True,
				"browseMode": True,
				"screenCurtain": True,
			},
		}

	def snapshot(self):
		return {
			key: dict(value) if isinstance(value, dict) else value
			for key, value in self.state.items()
		}

	def write_modes(self, values):
		self.state["modes"].update(values)


class StatusAdapterTests(unittest.TestCase):
	def setUp(self):
		self.backend = FakeBackend()
		self.adapter = StatusAdapter(self.backend)

	def test_status_is_read_only_summary_and_modes_declare_field_access(self):
		status = self.adapter.get_status()
		modes = self.adapter.get_modes()
		self.assertEqual("work", status["activeProfile"])
		self.assertTrue(modes["fields"]["browseMode"]["writable"])
		self.assertFalse(modes["fields"]["screenCurtain"]["writable"])

	def test_patch_requires_revision_and_only_writable_boolean_fields(self):
		before = self.adapter.get_modes()
		result = self.adapter.patch_modes({
			"baseRevision": before["revision"],
			"values": {"inputHelp": True, "browseMode": False},
		})
		self.assertTrue(result["changed"])
		self.assertTrue(result["state"]["values"]["inputHelp"])
		with self.assertRaises(StaleState):
			self.adapter.patch_modes({"baseRevision": before["revision"], "values": {"inputHelp": False}})
		with self.assertRaises(ValidationError):
			self.adapter.patch_modes({"baseRevision": result["revision"], "values": {"inputHelp": 1}})
		with self.assertRaises(ValidationError):
			self.adapter.patch_modes({"baseRevision": result["revision"], "values": {"screenCurtain": True}})

	def test_unavailable_context_mode_is_rejected(self):
		self.backend.state["modeAvailability"]["browseMode"] = False
		before = self.adapter.get_modes()
		with self.assertRaises(Conflict):
			self.adapter.patch_modes({"baseRevision": before["revision"], "values": {"browseMode": False}})


if __name__ == "__main__":
	unittest.main()
