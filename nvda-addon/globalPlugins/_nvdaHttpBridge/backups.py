"""Asynchronous complete portable NVDA backups."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import shutil
import threading
import time

from .config import (
	BACKUP_MAX_BYTES,
	BACKUP_MAX_CONCURRENT,
	BACKUP_MAX_DURATION_SECONDS,
	BACKUP_REAPER_INTERVAL_SECONDS,
	BACKUP_TTL_SECONDS,
	LEGACY_CREDENTIAL_FILE_NAME,
)
from .errors import Conflict, NotFound, SecureContext, ServiceUnavailable, TooManyRequests
from .ids import random_urlsafe


def _utc_after(seconds=0.0):
	return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


@dataclass
class BackupJob:
	id: str
	target_path: str
	backup_path: str
	created_at: float
	expires_at: float
	created_wall: str
	expires_wall: str
	status: str = "queued"
	started_at: float | None = None
	finished_at: float | None = None
	file_count: int = 0
	byte_count: int = 0
	error: dict | None = None
	output_published: bool = False

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
				"createdAt": self.created_wall,
				"expiresAt": self.expires_wall,
				"elapsedMs": round(max(0.0, elapsed) * 1000, 2),
				"fileCount": self.file_count,
				"bytes": self.byte_count,
				"error": self.error,
				"targetPath": self.target_path,
				"backupPath": self.backup_path,
			}


class BackupManager:
	def __init__(
		self,
		adapter,
		security_check,
		monotonic=None,
		reaper_interval=None,
		defer_start=False,
	):
		self._adapter = adapter
		self._security_check = security_check
		self._monotonic = monotonic or time.monotonic
		self._jobs = {}
		self._lock = threading.RLock()
		self._closing = False
		self._started = False
		self._reaper_interval = (
			BACKUP_REAPER_INTERVAL_SECONDS if reaper_interval is None else reaper_interval
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
			self._started = True
			self._reaper_thread = threading.Thread(
				target=self._reaper_loop,
				name="nvdaHttpBridgeBackupReaper",
				daemon=True,
			)
			self._reaper_thread.start()

	def create(self, target_path):
		self.start()
		self.cleanup_expired()
		self._security_check()
		target_path = self._normalize_target_path(target_path)
		backup_path = os.path.join(target_path, "nvda")
		now = self._monotonic()
		with self._lock:
			if self._closing:
				raise ServiceUnavailable()
			active = sum(job.status in ("queued", "running") for job in self._jobs.values())
			if active >= BACKUP_MAX_CONCURRENT:
				raise TooManyRequests("An NVDA backup is already running")
			if os.path.lexists(backup_path):
				raise Conflict("The NVDA backup path already exists")
			job_id = random_urlsafe(18)
			job = BackupJob(
				job_id,
				target_path,
				backup_path,
				now,
				now + BACKUP_TTL_SECONDS,
				_utc_after(),
				_utc_after(BACKUP_TTL_SECONDS),
			)
			self._jobs[job_id] = job
			thread = threading.Thread(target=self._run, args=(job,), name="nvdaHttpBridgeBackup", daemon=True)
			job.thread = thread
			thread.start()
			return job.snapshot(now)

	def _run(self, job):
		try:
			with job.lock:
				if job.cancel_event.is_set():
					job.status = "canceled"
					return
				job.status = "running"
				job.started_at = self._monotonic()
			self._security_check()
			os.makedirs(job.target_path, exist_ok=True)
			if os.path.lexists(job.backup_path):
				raise Conflict("The NVDA backup path already exists")
			try:
				os.mkdir(job.backup_path)
			except FileExistsError as error:
				raise Conflict("The NVDA backup path already exists") from error
			self._adapter.create_portable_copy(job.backup_path)
			self._remove_credentials(job.backup_path)
			if job.cancel_event.is_set():
				raise _Canceled()
			self._security_check()
			job.file_count, job.byte_count = self._measure_portable_copy(job)
			with job.lock:
				if job.cancel_event.is_set():
					raise _Canceled()
				job.output_published = True
				job.status = "completed"
				job.finished_at = self._monotonic()
				job.expires_at = job.finished_at + BACKUP_TTL_SECONDS
				job.expires_wall = _utc_after(BACKUP_TTL_SECONDS)
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
					"code": getattr(error, "code", "backupFailed"),
					"message": getattr(error, "message", "The NVDA backup could not be completed"),
				}
		finally:
			if job.status != "completed":
				self._delete_unpublished_output(job)

	def _measure_portable_copy(self, job):
		file_count = 0
		byte_count = 0
		for current, directories, files in os.walk(job.backup_path):
			directories.sort()
			files.sort()
			for file_name in files:
				if job.cancel_event.is_set():
					raise _Canceled()
				if self._monotonic() - job.started_at >= BACKUP_MAX_DURATION_SECONDS:
					raise Conflict("The NVDA backup exceeded its maximum lifetime")
				byte_count += os.path.getsize(os.path.join(current, file_name))
				if byte_count > BACKUP_MAX_BYTES:
					raise Conflict("The NVDA backup exceeded its size limit")
				file_count += 1
		return file_count, byte_count

	@staticmethod
	def _normalize_target_path(target_path):
		try:
			return os.path.abspath(os.path.expanduser(target_path))
		except (OSError, TypeError, ValueError) as error:
			raise Conflict("The NVDA backup target path is invalid") from error

	@staticmethod
	def _remove_credentials(backup_path):
		config_path = os.path.join(backup_path, "userConfig")
		for name in (LEGACY_CREDENTIAL_FILE_NAME, "nvdaHttpBridge-exports"):
			path = os.path.join(config_path, name)
			try:
				if os.path.isdir(path):
					shutil.rmtree(path)
				else:
					os.remove(path)
			except FileNotFoundError:
				pass

	def status(self, job_id):
		self.cleanup_expired()
		return self._get(job_id).snapshot(self._monotonic())

	def cancel(self, job_id):
		job = self._get(job_id)
		with job.lock:
			job.cancel_event.set()
			if job.status == "completed":
				job.status = "canceled"
				job.finished_at = self._monotonic()
		return job.snapshot(self._monotonic())

	def cancel_sensitive(self):
		with self._lock:
			jobs = list(self._jobs.values())
		for job in jobs:
			with job.lock:
				if job.status in ("queued", "running"):
					job.cancel_event.set()
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
			self._delete_unpublished_output(job)
			with self._lock:
				if self._jobs.get(job_id) is job:
					self._jobs.pop(job_id, None)

	def metrics(self):
		self.cleanup_expired()
		with self._lock:
			return {
				"active": sum(job.status in ("queued", "running") for job in self._jobs.values()),
				"jobs": len(self._jobs),
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
			job.cancel_event.set()
		for job in jobs:
			thread = job.thread
			if thread and thread.is_alive() and thread is not threading.current_thread():
				thread.join(1.0)
		for job in jobs:
			self._delete_unpublished_output(job)
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

	def _get(self, job_id):
		with self._lock:
			job = self._jobs.get(job_id)
		if job is None:
			raise NotFound("Unknown NVDA backup job")
		return job

	@staticmethod
	def _delete_unpublished_output(job):
		if job.output_published:
			return
		try:
			shutil.rmtree(job.backup_path)
		except FileNotFoundError:
			pass
		except OSError:
			pass


class _Canceled(Exception):
	pass
