import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from support import FakeAdapter, FakeClock, FakeNode, GLOBAL_PLUGINS

from _nvdaHttpBridge.config import EXPORT_TTL_SECONDS
from _nvdaHttpBridge.errors import Conflict, NotFound, SecureContext, TooManyRequests
import _nvdaHttpBridge.exports as exports_module
from _nvdaHttpBridge.exports import ExportManager
from _nvdaHttpBridge.serialization import ObjectRegistry
from _nvdaHttpBridge.tree import TreeOptions


class DirectExecutor:
	def call(self, callback, timeout_ms):
		return callback()


class SecurityGate:
	def __init__(self):
		self.restricted = False
		self.calls = 0

	def __call__(self, obj=None):
		self.calls += 1
		if self.restricted:
			raise SecureContext()


class PausingExecutor(DirectExecutor):
	"""Pause the first traversal batch, after the prepare call."""

	def __init__(self):
		self.calls = 0
		self.batch_entered = threading.Event()
		self.release_batch = threading.Event()

	def call(self, callback, timeout_ms):
		self.calls += 1
		if self.calls == 2:
			self.batch_entered.set()
			if not self.release_batch.wait(2.0):
				raise AssertionError("test did not release the export batch")
		return callback()


class RestrictAfterFirstBatchExecutor(DirectExecutor):
	def __init__(self, gate):
		self.calls = 0
		self.gate = gate

	def call(self, callback, timeout_ms):
		self.calls += 1
		result = callback()
		if self.calls == 2:
			self.gate.restricted = True
		return result


class ExportManagerTests(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.clock = FakeClock()
		self.adapter = FakeAdapter()
		self.registry = ObjectRegistry(adapter=self.adapter, monotonic=self.clock)
		self.gate = SecurityGate()
		self.root = FakeNode("根节点", children=[FakeNode("child")])
		self.managers = []

	def tearDown(self):
		for manager in reversed(self.managers):
			manager.close()

	def manager(self, executor=None):
		manager = ExportManager(
			executor or DirectExecutor(),
			lambda root_name: self.root,
			self.registry,
			self.gate,
			self.temp.name,
			adapter=self.adapter,
			monotonic=self.clock,
		)
		self.managers.append(manager)
		return manager

	def options(self, **changes):
		values = {
			"root": "focus",
			"depth": None,
			"max_children": None,
			"max_nodes": None,
			"timeout_ms": 500,
			"include": ("name", "description"),
			"format": "flat",
		}
		values.update(changes)
		return TreeOptions(**values)

	def wait_for_job(self, manager, job_id):
		job = manager._jobs[job_id]
		job.thread.join(2.0)
		self.assertFalse(job.thread.is_alive(), "export worker did not terminate")
		return manager.status(job_id), job

	def test_completed_export_is_line_delimited_json_with_local_errors(self):
		self.root.field_errors["description"] = RuntimeError("property vanished")
		manager = self.manager()

		created = manager.create(self.options())
		status, job = self.wait_for_job(manager, created["jobId"])

		self.assertEqual("completed", status["status"])
		self.assertEqual(2, status["nodeCount"])
		self.assertEqual(1, status["errorCount"])
		self.assertEqual(0, status["frontierSize"])
		self.assertTrue(status["download"].endswith("/data"))
		self.assertTrue(os.path.isfile(job.data_path))
		self.assertFalse(os.path.exists(job.part_path))
		with open(manager.data_path(job.id), "r", encoding="utf-8") as result_file:
			lines = result_file.readlines()
		self.assertEqual(2, len(lines))
		self.assertTrue(all(line.endswith("\n") for line in lines))
		records = [json.loads(line) for line in lines]
		self.assertEqual(["根节点", "child"], [record["object"]["name"] for record in records])
		self.assertEqual("RuntimeError", records[0]["object"]["errors"]["description"]["type"])

	def test_running_cancel_stops_before_next_batch_and_deletes_partial_file(self):
		self.root = FakeNode("root", children=[FakeNode("child-%s" % index) for index in range(40)])
		executor = PausingExecutor()
		manager = self.manager(executor)
		created = manager.create(self.options())
		self.assertTrue(executor.batch_entered.wait(2.0), "export did not enter its first batch")

		cancel_snapshot = manager.cancel(created["jobId"])
		self.assertEqual("running", cancel_snapshot["status"])
		executor.release_batch.set()
		status, job = self.wait_for_job(manager, created["jobId"])

		self.assertEqual("canceled", status["status"])
		self.assertLessEqual(status["nodeCount"], 25)
		self.assertFalse(os.path.exists(job.part_path))
		self.assertFalse(os.path.exists(job.data_path))

	def test_security_change_between_batches_fails_and_removes_partial_data(self):
		self.root = FakeNode("root", children=[FakeNode("child-%s" % index) for index in range(40)])
		manager = self.manager(RestrictAfterFirstBatchExecutor(self.gate))

		created = manager.create(self.options())
		status, job = self.wait_for_job(manager, created["jobId"])

		self.assertEqual("failed", status["status"])
		self.assertEqual("secureContext", status["error"]["code"])
		self.assertGreater(status["nodeCount"], 0)
		self.assertFalse(os.path.exists(job.part_path))
		self.assertFalse(os.path.exists(job.data_path))

	def test_completed_export_expires_and_is_removed_from_registry_and_disk(self):
		manager = self.manager()
		created = manager.create(self.options())
		_status, job = self.wait_for_job(manager, created["jobId"])
		self.assertTrue(os.path.isfile(job.data_path))

		self.clock.advance(EXPORT_TTL_SECONDS + 1)
		manager.cleanup_expired()

		self.assertFalse(os.path.exists(job.data_path))
		with self.assertRaises(NotFound):
			manager.status(job.id)

	def test_canceling_completed_export_changes_terminal_state_and_cleans_file(self):
		manager = self.manager()
		created = manager.create(self.options())
		_status, job = self.wait_for_job(manager, created["jobId"])

		canceled = manager.cancel(job.id)

		self.assertEqual("canceled", canceled["status"])
		self.assertIsNone(canceled["download"])
		self.assertFalse(os.path.exists(job.data_path))
		self.assertEqual("canceled", manager.status(job.id)["status"])
		with self.assertRaisesRegex(Conflict, "not ready"):
			manager.data_path(job.id)

	def test_startup_removes_orphan_export_files_only_from_dedicated_directory(self):
		export_directory = os.path.join(self.temp.name, "nvda-http-bridge", "exports")
		os.makedirs(export_directory, exist_ok=True)
		orphan_part = os.path.join(export_directory, "orphan.part")
		orphan_data = os.path.join(export_directory, "orphan.ndjson")
		outside_file = os.path.join(self.temp.name, "keep.ndjson")
		for path in (orphan_part, orphan_data, outside_file):
			with open(path, "w", encoding="utf-8") as output:
				output.write("orphan")

		self.manager()

		self.assertFalse(os.path.exists(orphan_part))
		self.assertFalse(os.path.exists(orphan_data))
		self.assertTrue(os.path.exists(outside_file))

	def test_background_reaper_removes_expired_completed_export_without_api_traffic(self):
		with mock.patch.object(exports_module, "EXPORT_REAPER_INTERVAL_SECONDS", 0.01, create=True):
			manager = self.manager()
			created = manager.create(self.options())
			_status, job = self.wait_for_job(manager, created["jobId"])
			self.assertTrue(os.path.isfile(job.data_path))

			self.clock.advance(EXPORT_TTL_SECONDS + 1)
			deadline = time.monotonic() + 1.0
			while job.id in manager._jobs and time.monotonic() < deadline:
				time.sleep(0.01)

		self.assertNotIn(job.id, manager._jobs)
		self.assertFalse(os.path.exists(job.data_path))

	def test_retained_job_count_quota_rejects_a_new_export(self):
		manager = self.manager()
		created = manager.create(self.options())
		_status, retained_job = self.wait_for_job(manager, created["jobId"])

		with mock.patch.object(exports_module, "EXPORT_MAX_RETAINED_JOBS", 1):
			with self.assertRaisesRegex(TooManyRequests, "retained export limit"):
				manager.create(self.options())

		self.assertEqual("completed", manager.status(retained_job.id)["status"])
		self.assertEqual(1, manager.metrics()["retained"])

	def test_total_byte_quota_rejects_creation_when_already_exhausted(self):
		manager = self.manager()
		created = manager.create(self.options())
		_status, retained_job = self.wait_for_job(manager, created["jobId"])
		self.assertGreater(retained_job.byte_count, 0)

		with mock.patch.object(
			exports_module,
			"EXPORT_MAX_TOTAL_BYTES",
			retained_job.byte_count,
		):
			with self.assertRaisesRegex(TooManyRequests, "storage quota"):
				manager.create(self.options())

		self.assertEqual(retained_job.byte_count, manager.metrics()["retainedBytes"])

	def test_total_byte_quota_is_rechecked_while_a_new_export_is_written(self):
		manager = self.manager()
		created = manager.create(self.options())
		_status, retained_job = self.wait_for_job(manager, created["jobId"])
		# One byte remains, so create() is allowed, but even the smallest NDJSON
		# record must be rejected by the worker before it is retained.
		with mock.patch.object(
			exports_module,
			"EXPORT_MAX_TOTAL_BYTES",
			retained_job.byte_count + 1,
		):
			second = manager.create(self.options())
			status, failed_job = self.wait_for_job(manager, second["jobId"])

		self.assertEqual("failed", status["status"])
		self.assertEqual("conflict", status["error"]["code"])
		self.assertIn("total export storage quota", status["error"]["message"])
		self.assertEqual(0, status["nodeCount"])
		self.assertFalse(os.path.exists(failed_job.part_path))
		self.assertFalse(os.path.exists(failed_job.data_path))

	def test_cancel_sensitive_revokes_in_memory_and_reaper_deletes_off_thread(self):
		manager = self.manager()
		created = manager.create(self.options())
		_status, job = self.wait_for_job(manager, created["jobId"])
		self.assertTrue(os.path.isfile(job.data_path))
		caller_thread = threading.get_ident()
		remove_calls = []
		real_remove = os.remove

		def tracked_remove(path):
			remove_calls.append((threading.get_ident(), threading.current_thread().name, path))
			return real_remove(path)

		with mock.patch.object(exports_module.os, "remove", side_effect=tracked_remove):
			manager.cancel_sensitive()
			self.assertEqual("canceled", job.status)
			self.assertFalse(manager.is_downloadable(job.id))
			deadline = time.monotonic() + 1.0
			while os.path.exists(job.data_path) and time.monotonic() < deadline:
				time.sleep(0.01)

		self.assertFalse(os.path.exists(job.data_path))
		self.assertTrue(remove_calls)
		self.assertFalse(any(thread_id == caller_thread for thread_id, _name, _path in remove_calls))
		self.assertTrue(any(name == "nvdaHttpBridgeExportReaper" for _thread_id, name, _path in remove_calls))

	def test_open_data_returns_open_handle_and_fstat_size_then_cancel_blocks_reopen(self):
		manager = self.manager()
		created = manager.create(self.options())
		_status, job = self.wait_for_job(manager, created["jobId"])

		data_file, length = manager.open_data(job.id)
		try:
			self.assertFalse(data_file.closed)
			self.assertEqual(os.fstat(data_file.fileno()).st_size, length)
			self.assertEqual(job.byte_count, length)
			self.assertEqual(length, len(data_file.read()))
		finally:
			data_file.close()

		manager.cancel(job.id)
		with self.assertRaisesRegex(Conflict, "not ready"):
			manager.open_data(job.id)

	def test_open_data_rejects_running_and_expired_jobs(self):
		self.root = FakeNode("root", children=[FakeNode("child-%s" % index) for index in range(40)])
		executor = PausingExecutor()
		manager = self.manager(executor)
		created = manager.create(self.options())
		self.assertTrue(executor.batch_entered.wait(2.0), "export did not enter its first batch")
		with self.assertRaisesRegex(Conflict, "not ready"):
			manager.open_data(created["jobId"])
		manager.cancel(created["jobId"])
		executor.release_batch.set()
		self.wait_for_job(manager, created["jobId"])

		completed = manager.create(self.options())
		_status, completed_job = self.wait_for_job(manager, completed["jobId"])
		self.clock.advance(EXPORT_TTL_SECONDS + 1)
		with self.assertRaises(NotFound):
			manager.open_data(completed_job.id)


if __name__ == "__main__":
	unittest.main()
