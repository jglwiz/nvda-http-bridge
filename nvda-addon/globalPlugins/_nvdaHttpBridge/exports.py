"""Asynchronous, batched NDJSON tree exports."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import os
import threading
import time

from .config import (
	EXPORT_BATCH_BUDGET_MS,
	EXPORT_BATCH_CALL_TIMEOUT_MS,
	EXPORT_BATCH_NODES,
	EXPORT_DIRECTORY_NAME,
	EXPORT_MAX_BYTES,
	EXPORT_MAX_CHILDREN,
	EXPORT_MAX_CONCURRENT,
	EXPORT_MAX_DEPTH,
	EXPORT_MAX_DURATION_SECONDS,
	EXPORT_MAX_NODES,
	EXPORT_MAX_RETAINED_JOBS,
	EXPORT_MAX_TOTAL_BYTES,
	EXPORT_REAPER_INTERVAL_SECONDS,
	EXPORT_TTL_SECONDS,
)
from .errors import Conflict, NotFound, SecureContext, ServiceUnavailable, TooManyRequests
from .ids import random_urlsafe
from .tree import TreeWalker


def _utc_after(seconds=0.0):
	return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


@dataclass
class ExportJob:
	id: str
	options: object
	part_path: str
	data_path: str
	created_at: float
	expires_at: float
	created_wall: str
	expires_wall: str
	status: str = "queued"
	started_at: float | None = None
	finished_at: float | None = None
	generation: str | None = None
	node_count: int = 0
	byte_count: int = 0
	error_count: int = 0
	frontier_size: int = 0
	truncation_reasons: tuple = ()
	error: dict | None = None

	def __post_init__(self):
		self.cancel_event = threading.Event()
		self.lock = threading.RLock()
		self.thread = None

	def snapshot(self, now):
		with self.lock:
			elapsed = 0.0
			if self.started_at is not None:
				elapsed = (self.finished_at or now) - self.started_at
			return {
				"jobId": self.id,
				"status": self.status,
				"generation": self.generation,
				"createdAt": self.created_wall,
				"expiresAt": self.expires_wall,
				"elapsedMs": round(max(0.0, elapsed) * 1000, 2),
				"nodeCount": self.node_count,
				"bytes": self.byte_count,
				"errorCount": self.error_count,
				"frontierSize": self.frontier_size,
				"truncated": bool(self.truncation_reasons),
				"truncationReasons": list(self.truncation_reasons),
				"error": self.error,
				"download": "/v1/tree/exports/%s/data" % self.id if self.status == "completed" else None,
			}


class ExportManager:
	def __init__(
		self,
		executor,
		root_provider,
		registry,
		security_check,
		export_root,
		adapter=None,
		monotonic=None,
		reaper_interval=None,
		defer_start=False,
	):
		self._executor = executor
		self._root_provider = root_provider
		self._registry = registry
		self._security_check = security_check
		self._export_root = os.path.join(export_root, EXPORT_DIRECTORY_NAME, "exports")
		self._adapter = adapter or registry.adapter
		self._monotonic = monotonic or time.monotonic
		self._jobs = {}
		self._lock = threading.RLock()
		self._closing = False
		self._started = False
		self._reaper_interval = (
			EXPORT_REAPER_INTERVAL_SECONDS if reaper_interval is None else reaper_interval
		)
		self._reaper_wake = threading.Event()
		self._reaper_thread = None
		if not defer_start:
			self.start()

	def start(self):
		with self._lock:
			if self._started:
				return
			if self._closing:
				raise ServiceUnavailable()
			# This is called only after the HTTP port has been bound, so a second
			# plugin instance cannot delete files owned by the live instance.
			self._cleanup_orphans()
			self._started = True
			self._reaper_thread = threading.Thread(
				target=self._reaper_loop,
				name="nvdaHttpBridgeExportReaper",
				daemon=True,
			)
			self._reaper_thread.start()

	def create(self, options):
		self.start()
		self.cleanup_expired()
		self._security_check()
		now = self._monotonic()
		with self._lock:
			if self._closing:
				raise ServiceUnavailable()
			active = sum(job.status in ("queued", "running") for job in self._jobs.values())
			if active >= EXPORT_MAX_CONCURRENT:
				raise TooManyRequests("An export is already running")
			retained = [job for job in self._jobs.values() if job.status == "completed"]
			if len(retained) >= EXPORT_MAX_RETAINED_JOBS:
				raise TooManyRequests("The retained export limit was reached; delete or wait for expiry")
			if sum(job.byte_count for job in retained) >= EXPORT_MAX_TOTAL_BYTES:
				raise TooManyRequests("The total export storage quota was reached")
			os.makedirs(self._export_root, exist_ok=True)
			job_id = random_urlsafe(18)
			part_path = os.path.join(self._export_root, job_id + ".part")
			data_path = os.path.join(self._export_root, job_id + ".ndjson")
			job = ExportJob(
				job_id,
				options,
				part_path,
				data_path,
				now,
				now + EXPORT_TTL_SECONDS,
				_utc_after(),
				_utc_after(EXPORT_TTL_SECONDS),
			)
			self._jobs[job_id] = job
			thread = threading.Thread(target=self._run, args=(job,), name="nvdaHttpBridgeExport", daemon=True)
			job.thread = thread
			thread.start()
			return job.snapshot(now)

	def _effective_options(self, options):
		return replace(
			options,
			depth=EXPORT_MAX_DEPTH if options.depth is None else min(options.depth, EXPORT_MAX_DEPTH),
			max_children=(
				EXPORT_MAX_CHILDREN
				if options.max_children is None
				else min(options.max_children, EXPORT_MAX_CHILDREN)
			),
			max_nodes=EXPORT_MAX_NODES if options.max_nodes is None else min(options.max_nodes, EXPORT_MAX_NODES),
			format="flat",
		)

	def _run(self, job):
		writer = None
		try:
			with job.lock:
				if job.cancel_event.is_set():
					job.status = "canceled"
					return
				job.status = "running"
				job.started_at = self._monotonic()

			def prepare():
				self._security_check()
				root = self._root_provider(job.options.root)
				self._security_check(root)
				return root, self._registry.new_generation()

			root, generation = self._executor.call(prepare, EXPORT_BATCH_CALL_TIMEOUT_MS)
			job.generation = generation
			walker = TreeWalker(
				root,
				self._effective_options(job.options),
				self._registry,
				generation,
				self._adapter,
			)
			writer = open(job.part_path, "x", encoding="utf-8", newline="\n")
			with self._lock:
				retained_bytes_at_start = sum(
					item.byte_count for item in self._jobs.values()
					if item is not job and item.status == "completed"
				)

			while not walker.done:
				if job.cancel_event.is_set():
					raise _Canceled()
				if self._monotonic() - job.started_at >= EXPORT_MAX_DURATION_SECONDS:
					raise Conflict("The export exceeded its maximum lifetime")

				def next_batch():
					self._security_check(root)
					return walker.next_batch(EXPORT_BATCH_NODES, EXPORT_BATCH_BUDGET_MS)

				records = self._executor.call(next_batch, EXPORT_BATCH_CALL_TIMEOUT_MS)
				if not records and not walker.done:
					# Let NVDA and the OS process other work before requesting another slice.
					time.sleep(0.001)
					continue
				for record in records:
					line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
					line_bytes = len(line.encode("utf-8"))
					if job.byte_count + line_bytes > EXPORT_MAX_BYTES:
						raise Conflict("The export exceeded its temporary-file quota")
					if retained_bytes_at_start + job.byte_count + line_bytes > EXPORT_MAX_TOTAL_BYTES:
						raise Conflict("The total export storage quota was exceeded")
					writer.write(line)
					job.byte_count += line_bytes
					job.node_count += 1
					if record["object"].get("errors"):
						job.error_count += 1
				writer.flush()
				with job.lock:
					job.frontier_size = walker.frontier_size()
					job.truncation_reasons = tuple(walker.reasons)
				time.sleep(0)

			if job.cancel_event.is_set():
				raise _Canceled()

			# Re-check the object and security context after the last batch. Without
			# this barrier, a lock/cancel racing the final batch could still rename a
			# sensitive partial file and publish it as completed.
			def final_security_check():
				self._security_check(root)

			self._executor.call(final_security_check, EXPORT_BATCH_CALL_TIMEOUT_MS)
			writer.close()
			writer = None
			with job.lock:
				if job.cancel_event.is_set():
					raise _Canceled()
				os.replace(job.part_path, job.data_path)
				job.status = "completed"
				job.finished_at = self._monotonic()
				job.expires_at = job.finished_at + EXPORT_TTL_SECONDS
				job.expires_wall = _utc_after(EXPORT_TTL_SECONDS)
				job.frontier_size = 0
				job.truncation_reasons = tuple(walker.reasons)
		except _Canceled:
			with job.lock:
				job.status = "canceled"
				job.finished_at = self._monotonic()
		except SecureContext as error:
			with job.lock:
				job.status = "failed"
				job.finished_at = self._monotonic()
				job.error = error.as_dict()["error"]
		except BaseException as error:
			with job.lock:
				job.status = "failed"
				job.finished_at = self._monotonic()
				job.error = {
					"code": getattr(error, "code", "exportFailed"),
					"message": getattr(error, "message", "The export could not be completed"),
				}
		finally:
			if writer is not None:
				writer.close()
			if job.status != "completed":
				self._delete_files(job)

	def status(self, job_id):
		self.cleanup_expired()
		job = self._get(job_id)
		return job.snapshot(self._monotonic())

	def data_path(self, job_id):
		self.cleanup_expired()
		job = self._get(job_id)
		with job.lock:
			if job.status != "completed" or not os.path.isfile(job.data_path):
				raise Conflict("The export data is not ready")
			return job.data_path

	def open_data(self, job_id):
		"""Acquire an already-open download handle before HTTP headers are sent."""
		self.cleanup_expired()
		job = self._get(job_id)
		now = self._monotonic()
		with job.lock:
			if (
				job.status != "completed"
				or job.cancel_event.is_set()
				or job.expires_at <= now
			):
				raise Conflict("The export data is not ready")
			try:
				data_file = open(job.data_path, "rb")
			except OSError:
				raise Conflict("The export data is not ready")
			try:
				length = os.fstat(data_file.fileno()).st_size
			except Exception:
				data_file.close()
				raise
			return data_file, length

	def is_downloadable(self, job_id):
		now = self._monotonic()
		with self._lock:
			job = self._jobs.get(job_id)
		if job is None:
			return False
		with job.lock:
			return (
				job.status == "completed"
				and not job.cancel_event.is_set()
				and job.expires_at > now
				and os.path.isfile(job.data_path)
			)

	def cancel(self, job_id):
		job = self._get(job_id)
		with job.lock:
			job.cancel_event.set()
			if job.status == "completed":
				self._delete_files(job)
				job.status = "canceled"
				job.finished_at = self._monotonic()
		return job.snapshot(self._monotonic())

	def cancel_sensitive(self):
		with self._lock:
			jobs = list(self._jobs.values())
		for job in jobs:
			with job.lock:
				job.cancel_event.set()
				if job.status == "completed":
					job.status = "canceled"
					job.finished_at = self._monotonic()
					job.expires_at = job.finished_at
					job.expires_wall = _utc_after()
			# Security callbacks execute on NVDA's main thread. The reaper performs
			# the actual file I/O after these in-memory revocations are visible.
		self._reaper_wake.set()

	def cleanup_expired(self):
		now = self._monotonic()
		with self._lock:
			expired = [
				(job_id, job) for job_id, job in self._jobs.items()
				if job.status not in ("queued", "running") and job.expires_at <= now
			]
		for job_id, job in expired:
			with job.lock:
				job.cancel_event.set()
				if job.status == "completed":
					job.status = "expired"
					job.finished_at = now
					job.expires_wall = _utc_after()
			if self._delete_files(job):
				with self._lock:
					if self._jobs.get(job_id) is job:
						self._jobs.pop(job_id, None)

	def metrics(self):
		self.cleanup_expired()
		with self._lock:
			retained = [job for job in self._jobs.values() if job.status == "completed"]
			return {
				"active": sum(job.status in ("queued", "running") for job in self._jobs.values()),
				"retained": len(retained),
				"retainedBytes": sum(job.byte_count for job in retained),
			}

	def close(self):
		with self._lock:
			if self._closing:
				return
			self._closing = True
			jobs = list(self._jobs.values())
		self._reaper_wake.set()
		reaper = self._reaper_thread
		if reaper and reaper.is_alive() and reaper is not threading.current_thread():
			reaper.join(1.0)
		for job in jobs:
			with job.lock:
				job.cancel_event.set()
		for job in jobs:
			thread = job.thread
			if thread and thread.is_alive() and thread is not threading.current_thread():
				thread.join(1.0)
		cleanup_thread = threading.Thread(
			target=lambda: [self._delete_files(job) for job in jobs],
			name="nvdaHttpBridgeExportCleanup",
			daemon=True,
		)
		cleanup_thread.start()
		cleanup_thread.join(1.0)
		with self._lock:
			self._jobs.clear()

	def _reaper_loop(self):
		while True:
			self._reaper_wake.wait(self._reaper_interval)
			self._reaper_wake.clear()
			with self._lock:
				if self._closing:
					return
			self.cleanup_expired()

	def _cleanup_orphans(self):
		try:
			entries = list(os.scandir(self._export_root))
		except FileNotFoundError:
			return
		except OSError:
			return
		for entry in entries:
			try:
				if entry.is_file() and entry.name.endswith((".part", ".ndjson")):
					os.remove(entry.path)
			except OSError:
				pass

	def _get(self, job_id):
		with self._lock:
			job = self._jobs.get(job_id)
		if job is None:
			raise NotFound("Unknown export job")
		return job

	@staticmethod
	def _delete_files(job):
		removed = True
		for path in (job.part_path, job.data_path):
			try:
				os.remove(path)
			except FileNotFoundError:
				pass
			except OSError:
				removed = False
		return removed


class _Canceled(Exception):
	pass
