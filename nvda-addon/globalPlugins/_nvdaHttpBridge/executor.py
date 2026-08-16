"""Deadline-aware execution on NVDA's main event queue."""

import itertools
import threading
import time

from .errors import MainThreadTimeout, ServiceUnavailable, TooManyRequests


class _Task:
	def __init__(self, task_id, func, deadline, created_at):
		self.id = task_id
		self.func = func
		self.deadline = deadline
		self.created_at = created_at
		self.started_at = None
		self.finished_at = None
		self.state = "pending"
		self.result = None
		self.error = None
		self.done = threading.Event()
		self.lock = threading.Lock()

	def cancel_pending(self, reason, now=None):
		with self.lock:
			if self.state != "pending":
				return False
			self.state = "canceled"
			self.error = reason
			self.finished_at = time.monotonic() if now is None else now
			self.done.set()
			return True


class MainThreadExecutor:
	"""Submit bounded calls to an injected NVDA main-thread scheduler.

	The scheduler accepts one no-argument callable. A task that expires while
	still pending is never executed. A task already inside a COM call cannot be
	forcibly interrupted; callers still receive a timeout and the task is cleaned
	up when the call eventually returns.
	"""

	def __init__(self, scheduler, max_pending=64, monotonic=None):
		self._scheduler = scheduler
		self._max_pending = max_pending
		self._monotonic = monotonic or time.monotonic
		self._ids = itertools.count(1)
		self._tasks = {}
		self._lock = threading.RLock()
		self._closing = False
		self._last_timeout_at = None
		self._last_execution_at = None
		self._last_queue_wait_ms = None
		self._last_main_thread_ms = None

	def call(self, func, timeout_ms):
		now = self._monotonic()
		deadline = now + (max(1, timeout_ms) / 1000.0)
		with self._lock:
			if self._closing:
				raise ServiceUnavailable()
			if len(self._tasks) >= self._max_pending:
				raise TooManyRequests("The NVDA main-thread queue is full")
			task = _Task(next(self._ids), func, deadline, now)
			self._tasks[task.id] = task

		try:
			self._scheduler(lambda: self._run(task))
		except Exception:
			with self._lock:
				self._tasks.pop(task.id, None)
			raise ServiceUnavailable("Unable to schedule work on the NVDA main thread")

		remaining = max(0.0, deadline - self._monotonic())
		if not task.done.wait(remaining):
			timeout = MainThreadTimeout(details={"taskId": task.id})
			canceled_pending = task.cancel_pending(timeout, self._monotonic())
			with self._lock:
				self._last_timeout_at = self._monotonic()
				if canceled_pending:
					# The scheduled wrapper still holds the task object and will safely
					# no-op if it eventually runs; it need not consume queue capacity.
					self._tasks.pop(task.id, None)
			raise timeout

		if task.error is not None:
			raise task.error
		return task.result

	def _run(self, task):
		try:
			with task.lock:
				if task.state != "pending":
					return
				now = self._monotonic()
				if self._closing or now >= task.deadline:
					task.state = "canceled"
					task.error = MainThreadTimeout(details={"taskId": task.id, "expiredBeforeStart": True})
					task.finished_at = now
					task.done.set()
					return
				task.state = "running"
				task.started_at = now
			try:
				result = task.func()
			except BaseException as error:
				with task.lock:
					task.error = error
			else:
				with task.lock:
					task.result = result
			finally:
				with task.lock:
					task.state = "finished"
					task.finished_at = self._monotonic()
					task.done.set()
		finally:
			with self._lock:
				if task.started_at is not None:
					self._last_queue_wait_ms = round((task.started_at - task.created_at) * 1000, 2)
				if task.started_at is not None and task.finished_at is not None:
					self._last_main_thread_ms = round((task.finished_at - task.started_at) * 1000, 2)
					self._last_execution_at = task.finished_at
				self._tasks.pop(task.id, None)

	def close(self):
		with self._lock:
			if self._closing:
				return
			self._closing = True
			tasks = list(self._tasks.values())
		for task in tasks:
			task.cancel_pending(ServiceUnavailable("The bridge is shutting down"), self._monotonic())

	def metrics(self):
		with self._lock:
			states = [task.state for task in self._tasks.values()]
			return {
				"pending": states.count("pending"),
				"running": states.count("running"),
				"closing": self._closing,
				"lastTimeoutMonotonic": self._last_timeout_at,
				"lastExecutionMonotonic": self._last_execution_at,
				"lastQueueWaitMs": self._last_queue_wait_ms,
				"lastMainThreadMs": self._last_main_thread_ms,
			}
