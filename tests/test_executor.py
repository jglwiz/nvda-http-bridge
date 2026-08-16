import unittest

from support import GLOBAL_PLUGINS  # noqa: F401

from _nvdaHttpBridge.errors import MainThreadTimeout, ServiceUnavailable
from _nvdaHttpBridge.executor import MainThreadExecutor


class MainThreadExecutorTests(unittest.TestCase):
	def test_task_that_times_out_while_pending_never_executes(self):
		scheduled = []
		executed = []
		executor = MainThreadExecutor(scheduled.append)

		with self.assertRaises(MainThreadTimeout):
			executor.call(lambda: executed.append(True), timeout_ms=10)

		self.assertEqual(1, len(scheduled))
		scheduled.pop()()
		self.assertEqual([], executed)
		self.assertEqual(0, executor.metrics()["pending"])
		self.assertEqual(0, executor.metrics()["running"])

	def test_scheduler_failure_is_reported_as_service_unavailable(self):
		def fail_to_schedule(_callback):
			raise RuntimeError("scheduler stopped")

		executor = MainThreadExecutor(fail_to_schedule)
		with self.assertRaises(ServiceUnavailable):
			executor.call(lambda: None, timeout_ms=100)
		self.assertEqual(0, executor.metrics()["pending"])


if __name__ == "__main__":
	unittest.main()
