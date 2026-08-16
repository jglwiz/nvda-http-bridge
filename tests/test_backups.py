import os
import tempfile
import threading
import unittest

from support import FakeClock, GLOBAL_PLUGINS  # noqa: F401

from _nvdaHttpBridge.backups import BackupManager
from _nvdaHttpBridge.config import BACKUP_TTL_SECONDS, TOKEN_FILE_NAME
from _nvdaHttpBridge.errors import Conflict, NotFound, SecureContext, TooManyRequests


class SecurityGate:
	def __init__(self):
		self.restricted = False

	def __call__(self, obj=None):
		if self.restricted:
			raise SecureContext()


class BackupAdapter:
	def __init__(self):
		self.created = []

	def create_portable_copy(self, destination):
		self.created.append(destination)
		os.makedirs(os.path.join(destination, "userConfig", "addons"))
		with open(os.path.join(destination, "nvda.exe"), "wb") as output:
			output.write(b"portable")
		with open(os.path.join(destination, "userConfig", "nvda.ini"), "w", encoding="utf-8") as output:
			output.write("config")
		with open(os.path.join(destination, "userConfig", TOKEN_FILE_NAME), "w", encoding="utf-8") as output:
			output.write("secret")


class PausingAdapter(BackupAdapter):
	def __init__(self):
		super().__init__()
		self.entered = threading.Event()
		self.release = threading.Event()

	def create_portable_copy(self, destination):
		self.entered.set()
		if not self.release.wait(2.0):
			raise AssertionError("test did not release backup creation")
		super().create_portable_copy(destination)


class ReservedDestinationAdapter(BackupAdapter):
	def create_portable_copy(self, destination):
		if not os.path.isdir(destination):
			raise AssertionError("the backup directory must be reserved before NVDA writes")
		super().create_portable_copy(destination)


class BackupManagerTests(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.clock = FakeClock()
		self.gate = SecurityGate()
		self.managers = []

	def tearDown(self):
		for manager in reversed(self.managers):
			manager.close()

	def manager(self, adapter=None):
		manager = BackupManager(
			adapter or BackupAdapter(),
			self.gate,
			monotonic=self.clock,
		)
		self.managers.append(manager)
		return manager

	def wait_for_job(self, manager, job_id):
		job = manager._jobs[job_id]
		job.thread.join(3.0)
		self.assertFalse(job.thread.is_alive(), "backup worker did not terminate")
		return manager.status(job_id), job

	def test_target_path_creates_nvda_child_and_excludes_http_token(self):
		manager = self.manager()
		target_path = os.path.join(self.temp.name, "nested", "backups")
		created = manager.create(target_path)
		status, _job = self.wait_for_job(manager, created["jobId"])
		backup_path = os.path.join(os.path.abspath(target_path), "nvda")

		self.assertEqual("completed", status["status"])
		self.assertEqual(os.path.abspath(target_path), status["targetPath"])
		self.assertEqual(backup_path, status["backupPath"])
		self.assertEqual(2, status["fileCount"])
		self.assertGreater(status["bytes"], 0)
		self.assertTrue(os.path.isfile(os.path.join(backup_path, "nvda.exe")))
		self.assertTrue(os.path.isfile(os.path.join(backup_path, "userConfig", "nvda.ini")))
		self.assertFalse(os.path.exists(os.path.join(backup_path, "userConfig", TOKEN_FILE_NAME)))

		manager.cancel(created["jobId"])
		self.assertTrue(os.path.isdir(backup_path), "deleting the HTTP job must preserve the backup")

	def test_existing_nvda_child_is_refused(self):
		manager = self.manager()
		target_path = os.path.join(self.temp.name, "existing")
		os.makedirs(os.path.join(target_path, "nvda"))

		with self.assertRaises(Conflict):
			manager.create(target_path)

	def test_backup_path_is_reserved_before_nvda_writes(self):
		manager = self.manager(ReservedDestinationAdapter())
		created = manager.create(os.path.join(self.temp.name, "reserved"))
		status, _job = self.wait_for_job(manager, created["jobId"])

		self.assertEqual("completed", status["status"])

	def test_cancel_running_backup_removes_unpublished_output(self):
		adapter = PausingAdapter()
		manager = self.manager(adapter)
		target_path = os.path.join(self.temp.name, "cancel")
		created = manager.create(target_path)
		self.assertTrue(adapter.entered.wait(2.0))

		manager.cancel(created["jobId"])
		adapter.release.set()
		status, _job = self.wait_for_job(manager, created["jobId"])

		self.assertEqual("canceled", status["status"])
		self.assertFalse(os.path.exists(os.path.join(target_path, "nvda")))

	def test_only_running_jobs_count_toward_concurrency_limit(self):
		adapter = PausingAdapter()
		manager = self.manager(adapter)
		first = manager.create(os.path.join(self.temp.name, "first"))
		self.assertTrue(adapter.entered.wait(2.0))

		with self.assertRaises(TooManyRequests):
			manager.create(os.path.join(self.temp.name, "second"))

		manager.cancel(first["jobId"])
		adapter.release.set()
		self.wait_for_job(manager, first["jobId"])
		second = manager.create(os.path.join(self.temp.name, "second"))
		adapter.release.set()
		self.wait_for_job(manager, second["jobId"])

	def test_expiry_removes_job_but_preserves_completed_backup(self):
		manager = self.manager()
		target_path = os.path.join(self.temp.name, "durable")
		created = manager.create(target_path)
		_status, job = self.wait_for_job(manager, created["jobId"])
		self.clock.advance(BACKUP_TTL_SECONDS + 1)

		manager.cleanup_expired()

		self.assertTrue(os.path.isdir(os.path.join(target_path, "nvda")))
		with self.assertRaises(NotFound):
			manager.status(job.id)

	def test_restricted_context_rejects_creation(self):
		manager = self.manager()
		self.gate.restricted = True

		with self.assertRaises(SecureContext):
			manager.create(os.path.join(self.temp.name, "restricted"))


if __name__ == "__main__":
	unittest.main()
