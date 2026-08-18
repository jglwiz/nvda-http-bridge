import unittest
from unittest.mock import patch

from support import FakeAdapter, FakeNode, GLOBAL_PLUGINS

from _nvdaHttpBridge.auth import SecurityState
from _nvdaHttpBridge.config import SYNC_BATCH_NODES
from _nvdaHttpBridge.errors import (
	BadRequest,
	RestartAlreadyScheduled,
	RestartBlocked,
	StaleObject,
	UnsafeAction,
	ValidationError,
)
from _nvdaHttpBridge.events import EventBuffer, SpeechObserver
from _nvdaHttpBridge.serialization import ObjectRegistry
from _nvdaHttpBridge.service import BridgeService


class DirectExecutor:
	def call(self, callback, timeout_ms):
		return callback()

	def close(self):
		pass

	def metrics(self):
		return {"pending": 0, "running": 0, "closing": False}


class RecordingExecutor(DirectExecutor):
	"""Execute inline while recording which NVDA objects each slice touched."""

	def __init__(self, adapter):
		self.adapter = adapter
		self.calls = []

	def call(self, callback, timeout_ms):
		before = len(self.adapter.reads)
		try:
			return callback()
		finally:
			touched = {
				name
				for name, _field in self.adapter.reads[before:]
			}
			self.calls.append({"timeoutMs": timeout_ms, "objects": touched})


class ActionAdapter(FakeAdapter):
	def __init__(self):
		super().__init__()
		self.calls = []

	def assert_safe(self, obj=None):
		self.calls.append(("assert_safe", obj))

	def speak(self, text):
		self.calls.append(("speak", text))

	def cancel_speech(self):
		self.calls.append(("cancel_speech",))

	def execute_gesture(self, key):
		self.calls.append(("gesture", key))

	def focus_object(self, obj):
		self.calls.append(("focus", obj))

	def default_action(self, obj):
		self.calls.append(("default_action", obj))

	def assert_restart_allowed(self):
		self.calls.append(("assert_restart_allowed",))

	def schedule(self, callback):
		self.calls.append(("schedule", callback))
		callback()

	def restart(self):
		self.calls.append(("restart",))


class TreeAdapter(FakeAdapter):
	def __init__(self, root):
		super().__init__()
		self.root = root

	def assert_safe(self, obj=None):
		pass

	def get_root(self, root_name):
		self.requested_root = root_name
		return self.root


class FakeExports:
	def metrics(self):
		return {"active": 0, "retained": 0}

	def close(self):
		pass

	def cancel_sensitive(self):
		pass


class FakeBackups:
	def __init__(self):
		self.calls = []

	def metrics(self):
		return {"active": 0, "jobs": 0}

	def create(self, target_path):
		self.calls.append(("create", target_path))
		return {"jobId": "backup-1", "status": "queued"}

	def status(self, job_id):
		self.calls.append(("status", job_id))
		return {"jobId": job_id, "status": "completed"}

	def cancel(self, job_id):
		self.calls.append(("cancel", job_id))
		return {"jobId": job_id, "status": "canceled"}

	def cancel_sensitive(self):
		self.calls.append(("cancel_sensitive",))

	def close(self):
		self.calls.append(("close",))


class FakeTextAdapter:
	@staticmethod
	def limits():
		return {"maxChars": 100}

	def set_caret(self, obj, object_id, generation, body):
		return {"ok": True, "objectId": object_id, "generation": generation, "offset": body["offset"]}

	def set_selection(self, obj, object_id, generation, body):
		return {
			"ok": True, "objectId": object_id, "generation": generation,
			"start": body["start"], "end": body["end"],
		}


class FakeDiagnosticExports:
	def __init__(self):
		self.calls = []

	def metrics(self):
		return {"active": 0, "retained": 0}

	def create(self):
		self.calls.append(("create",))
		return {"jobId": "diagnostic-1", "status": "queued"}

	def cancel_sensitive(self):
		self.calls.append(("cancel_sensitive",))

	def close(self):
		self.calls.append(("close",))


class BridgeServiceActionTests(unittest.TestCase):
	def setUp(self):
		self.adapter = ActionAdapter()
		self.executor = DirectExecutor()
		self.registry = ObjectRegistry(adapter=self.adapter)
		self.events = EventBuffer()
		self.speech = SpeechObserver(self.events)
		self.backups = FakeBackups()
		self.service = BridgeService(
			self.adapter,
			self.executor,
			self.registry,
			self.events,
			self.speech,
			FakeExports(),
			SecurityState(),
			backups=self.backups,
		)

	def tearDown(self):
		self.events.close()

	def test_capabilities_declare_no_authentication(self):
		self.assertEqual(
			{"mode": "none"},
			self.service.capabilities()["auth"],
		)

	def test_speak_and_gesture_accept_only_whitelisted_fields(self):
		for action, body in (
			("speak", {}),
			("speak", {"text": "hello", "module": "os"}),
			("gesture", {"key": "NVDA+n", "repeat": 100}),
			("cancel-speech", {"unexpected": True}),
		):
			with self.subTest(action=action, body=body):
				with self.assertRaises(ValidationError):
					self.service.action(action, body)

		self.assertFalse(any(call[0] in {"speak", "gesture", "cancel_speech"} for call in self.adapter.calls))

	def test_valid_actions_are_dispatched_through_bounded_adapter_methods(self):
		self.assertEqual(
			{"ok": True, "text": "hello"},
			self.service.action("speak", {"text": "hello"}),
		)
		self.assertEqual(
			{"ok": True, "key": "NVDA+n"},
			self.service.action("gesture", {"key": "NVDA+n"}),
		)
		self.assertEqual({"ok": True}, self.service.action("cancel-speech", {}))
		self.assertIn(("speak", "hello"), self.adapter.calls)
		self.assertIn(("gesture", "NVDA+n"), self.adapter.calls)
		self.assertIn(("cancel_speech",), self.adapter.calls)

	def test_dangerous_gesture_aliases_and_modifier_order_are_rejected(self):
		blocked = (
			"control+NVDA+f3",
			"f3+nvda+ctrl",
			"capslock+control+f3",
			"ctrl+insert+f3",
			"numpadinsert+control+f3",
			"kb:ctrl+capslock+f3",
			"kb(desktop):q+insert",
			"q+NVDA",
		)
		for key in blocked:
			with self.subTest(key=key):
				with self.assertRaises(UnsafeAction):
					self.service.action("gesture", {"key": key})

		self.assertEqual(
			{"ok": True, "key": "tab"},
			self.service.action("gesture", {"key": "tab"}),
		)
		self.assertIn(("gesture", "tab"), self.adapter.calls)

	def test_restart_is_intentionally_unavailable_through_generic_action(self):
		with self.assertRaises(UnsafeAction) as caught:
			self.service.action("restart", {})
		self.assertEqual(409, caught.exception.status)
		self.assertEqual("unsafeAction", caught.exception.code)

	def test_dedicated_restart_prepares_once_and_schedules_native_restart(self):
		prepared = self.service.prepare_restart({})
		self.assertEqual("accepted", prepared["status"])
		self.assertEqual(1234, prepared["before"]["nvdaProcessId"])
		self.assertTrue(self.service.health()["restartPending"])
		with self.assertRaises(RestartAlreadyScheduled):
			self.service.prepare_restart({})
		self.assertTrue(self.service.schedule_prepared_restart(prepared["restartId"]))
		self.assertFalse(self.service.schedule_prepared_restart(prepared["restartId"]))
		self.assertFalse(self.service.schedule_prepared_restart("wrong"))
		self.assertEqual(1, self.adapter.calls.count(("restart",)))

	def test_dedicated_restart_rejects_non_empty_or_non_object_bodies(self):
		for body in ([], {"mode": "unsafe"}):
			with self.subTest(body=body), self.assertRaises(ValidationError):
				self.service.prepare_restart(body)
		self.assertFalse(any(call[0] == "schedule" for call in self.adapter.calls))

	def test_dedicated_restart_propagates_modal_precheck_without_reserving(self):
		def blocked():
			raise RestartBlocked()
		self.adapter.assert_restart_allowed = blocked
		with self.assertRaises(RestartBlocked):
			self.service.prepare_restart({})
		self.assertFalse(self.service.health()["restartPending"])

	def test_backup_requires_only_target_path_and_is_advertised(self):
		with self.assertRaises(ValidationError):
			self.service.create_backup({"unknown": "value"})
		with self.assertRaises(ValidationError):
			self.service.create_backup({})
		with self.assertRaises(ValidationError):
			self.service.create_backup({"targetPath": 123})
		created = self.service.create_backup({"targetPath": "D:/target"})

		self.assertEqual("backup-1", created["jobId"])
		self.assertEqual("/v1/backups", self.service.capabilities()["endpoints"]["backups"])
		self.assertIn(("create", "D:/target"), self.backups.calls)
		self.assertEqual("nvda", self.service.capabilities()["backupLimits"]["targetPathChildName"])

	def test_configuration_resources_are_thin_main_thread_orchestration(self):
		class Resource:
			def __init__(self):
				self.calls = []

			def get_general(self):
				self.calls.append("get_general")
				return {"revision": "general"}

			def patch_general(self, body):
				self.calls.append(("patch_general", body))
				return {"revision": "changed"}

		resource = Resource()
		self.service.settings = resource
		self.assertEqual({"revision": "general"}, self.service.general_settings())
		self.assertEqual({"revision": "changed"}, self.service.patch_general_settings({"values": {}}))
		self.assertEqual(["get_general", ("patch_general", {"values": {}})], resource.calls)
		config_caps = self.service.capabilities()["configurationResources"]
		self.assertFalse(config_caps["speechDictionaries"]["clearAllSupported"])
		self.assertFalse(config_caps["gestures"]["resetAllSupported"])

	def test_focus_allows_generation_to_be_omitted_but_rejects_wrong_generation(self):
		node = FakeNode("button")
		object_id = self.registry.register(node, "g1")

		result = self.service.action("focus", {"objectId": object_id})
		self.assertEqual("g1", result["generation"])
		self.assertIn(("focus", node), self.adapter.calls)

		with self.assertRaises(StaleObject):
			self.service.action("default-action", {"objectId": object_id, "generation": "wrong"})

	def test_text_actions_require_generation_and_are_dispatched_to_text_adapter(self):
		node = FakeNode("editor")
		object_id = self.registry.register(node, "g1")
		self.service.text_adapter = FakeTextAdapter()
		caret = self.service.action("set-caret", {
			"objectId": object_id, "generation": "g1", "baseRevision": "r1", "offset": 2,
		})
		selection = self.service.action("set-selection", {
			"objectId": object_id, "generation": "g1", "baseRevision": "r1", "start": 1, "end": 3,
		})
		self.assertEqual(2, caret["offset"])
		self.assertEqual((1, 3), (selection["start"], selection["end"]))
		with self.assertRaises(ValidationError):
			self.service.action("set-caret", {"objectId": object_id, "offset": 2})

	def test_diagnostic_export_accepts_only_empty_body_and_is_advertised(self):
		manager = FakeDiagnosticExports()
		self.service.diagnostic_exports = manager
		with self.assertRaises(ValidationError):
			self.service.create_diagnostic_export({"path": "D:/outside"})
		created = self.service.create_diagnostic_export({})
		self.assertEqual("diagnostic-1", created["jobId"])
		self.assertEqual("/v1/diagnostics/exports", self.service.capabilities()["endpoints"]["diagnosticExports"])

	def test_unknown_action_and_arbitrary_non_object_body_are_rejected(self):
		for action, body in (("import-module", {"name": "os"}), ("unknown", {})):
			with self.subTest(action=action):
				with self.assertRaises(ValidationError):
					self.service.action(action, body)
		with self.assertRaises(BadRequest) as caught:
			self.service.action("speak", ["hello"])
		self.assertEqual("badRequest", caught.exception.code)


class BridgeServiceTreeBatchTests(unittest.TestCase):
	def test_sync_tree_stops_at_serialized_result_size_limit(self):
		adapter = TreeAdapter(FakeNode("root", children=[FakeNode("child")]))
		executor = DirectExecutor()
		registry = ObjectRegistry(adapter=adapter)
		events = EventBuffer()
		service = BridgeService(
			adapter,
			executor,
			registry,
			events,
			SpeechObserver(events),
			FakeExports(),
			SecurityState(),
		)
		try:
			with patch("_nvdaHttpBridge.service.SYNC_MAX_RESULT_BYTES", 1):
				result = service.tree({
					"depth": "1",
					"maxChildren": "2",
					"maxNodes": "2",
					"include": "name",
					"format": "flat",
				})
		finally:
			events.close()

		self.assertTrue(result["truncated"])
		self.assertIn("sizeLimit", result["truncationReasons"])
		self.assertEqual(0, result["nodeCount"])
		self.assertEqual([], result["tree"])

	def test_sync_tree_reads_nodes_in_multiple_bounded_executor_slices(self):
		branches = [
			FakeNode(
				"branch-%d" % branch_index,
				children=[
					FakeNode("leaf-%d-%d" % (branch_index, leaf_index))
					for leaf_index in range(10)
				],
			)
			for branch_index in range(10)
		]
		adapter = TreeAdapter(FakeNode("root", children=branches))
		executor = RecordingExecutor(adapter)
		registry = ObjectRegistry(adapter=adapter)
		events = EventBuffer()
		service = BridgeService(
			adapter,
			executor,
			registry,
			events,
			SpeechObserver(events),
			FakeExports(),
			SecurityState(),
		)
		try:
			result = service.tree({
				"depth": "2",
				"maxChildren": "20",
				"maxNodes": "111",
				"timeoutMs": "3000",
				"include": "name",
				"format": "flat",
			})
		finally:
			events.close()

		self.assertEqual(111, result["nodeCount"])
		read_slices = [call["objects"] for call in executor.calls if call["objects"]]
		self.assertGreater(
			len(read_slices),
			1,
			"同步树遍历不能把所有 NVDAObject 读取塞进一次主线程调度",
		)
		self.assertLess(
			max(len(objects) for objects in read_slices),
			result["nodeCount"],
			"准备调用与遍历调用分开仍不够；节点读取本身也必须有界分批",
		)
		self.assertTrue(
			all(len(objects) <= SYNC_BATCH_NODES for objects in read_slices),
			"任一主线程切片触达的节点数都不能超过同步批次上限",
		)
		self.assertEqual(
			result["nodeCount"],
			len(set().union(*read_slices)),
		)


if __name__ == "__main__":
	unittest.main()
