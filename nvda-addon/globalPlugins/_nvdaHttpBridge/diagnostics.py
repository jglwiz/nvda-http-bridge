"""Read-only NVDA inventory and bounded asynchronous diagnostic bundles."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
import threading
import time
import zipfile

from .config import (
	DIAGNOSTICS_LOG_TAIL_BYTES,
	DIAGNOSTICS_MAX_BYTES,
	DIAGNOSTICS_MAX_CONCURRENT,
	DIAGNOSTICS_MAX_RETAINED_JOBS,
	DIAGNOSTICS_REAPER_INTERVAL_SECONDS,
	DIAGNOSTICS_TTL_SECONDS,
	EXPORT_DIRECTORY_NAME,
)
from .errors import Conflict, NotFound, SecureContext, ServiceUnavailable, TooManyRequests
from .ids import random_urlsafe
from .serialization import safe_text


def _utc_after(seconds=0.0):
	return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


class NvdaDiagnosticsBackend:
	def addons(self):
		import addonHandler

		items = []
		for addon in addonHandler.getAvailableAddons():
			manifest = getattr(addon, "manifest", {})
			items.append({
				"name": safe_text(manifest.get("name", getattr(addon, "name", "")), 256) or "",
				"summary": safe_text(manifest.get("summary", ""), 1024) or "",
				"version": safe_text(manifest.get("version", getattr(addon, "version", "")), 128) or "",
				"author": safe_text(manifest.get("author", ""), 512) or "",
				"running": bool(getattr(addon, "isRunning", False)),
				"disabled": bool(getattr(addon, "isDisabled", False)),
				"compatible": bool(getattr(addon, "isCompatible", False)),
				"overrideCompatibility": bool(getattr(addon, "overrideIncompatibility", False)),
			})
		return sorted(items, key=lambda item: item["name"].casefold())

	def global_plugins(self):
		import globalPluginHandler

		items = []
		for plugin in globalPluginHandler.runningPlugins:
			plugin_class = plugin.__class__
			items.append({
				"module": safe_text(plugin_class.__module__, 512),
				"class": safe_text(plugin_class.__name__, 256),
			})
		return sorted(items, key=lambda item: (item["module"].casefold(), item["class"].casefold()))

	def drivers(self):
		import braille
		from synthDriverHandler import getSynth, getSynthList

		synth = getSynth()
		display = getattr(getattr(braille, "handler", None), "display", None)
		return {
			"synthesizers": {
				"active": getattr(synth, "name", None),
				"available": [
					{"id": safe_text(name, 256), "displayName": safe_text(label, 512)}
					for name, label in getSynthList()
				],
			},
			"brailleDisplays": {
				"active": getattr(display, "name", None),
				"available": [
					{"id": safe_text(name, 256), "displayName": safe_text(label, 512)}
					for name, label in braille.getDisplayList()
				],
			},
		}

	def snapshot(self):
		return {
			"addons": self.addons(),
			"globalPlugins": self.global_plugins(),
			"drivers": self.drivers(),
		}


class DiagnosticsAdapter:
	def __init__(self, backend=None):
		self.backend = backend or NvdaDiagnosticsBackend()

	def addons(self):
		items = self.backend.addons()
		return {"items": items, "count": len(items)}

	def global_plugins(self):
		items = self.backend.global_plugins()
		return {"items": items, "count": len(items)}

	def drivers(self):
		return self.backend.drivers()

	def snapshot(self):
		return self.backend.snapshot()


@dataclass
class DiagnosticsJob:
	id: str
	part_path: str
	data_path: str
	created_at: float
	expires_at: float
	created_wall: str
	expires_wall: str
	status: str = "queued"
	started_at: float | None = None
	finished_at: float | None = None
	byte_count: int = 0
	error: dict | None = None

	def __post_init__(self):
		self.cancel_event = threading.Event()
		self.lock = threading.RLock()
		self.thread = None

	def snapshot(self, now):
		with self.lock:
			elapsed = 0.0 if self.started_at is None else (self.finished_at or now) - self.started_at
			return {
				"jobId": self.id,
				"status": self.status,
				"createdAt": self.created_wall,
				"expiresAt": self.expires_wall,
				"elapsedMs": round(max(0.0, elapsed) * 1000, 2),
				"bytes": self.byte_count,
				"error": self.error,
				"download": "/v1/diagnostics/exports/%s/data" % self.id if self.status == "completed" else None,
			}


class DiagnosticsExportManager:
	def __init__(self, executor, adapter, security_check, export_root, log_path_provider=None,
			monotonic=None, reaper_interval=None, defer_start=False):
		self._executor = executor
		self._adapter = adapter
		self._security_check = security_check
		self._root = os.path.join(export_root, EXPORT_DIRECTORY_NAME, "diagnostics")
		self._log_path_provider = log_path_provider
		self._monotonic = monotonic or time.monotonic
		self._jobs = {}
		self._lock = threading.RLock()
		self._closing = False
		self._started = False
		self._reaper_interval = DIAGNOSTICS_REAPER_INTERVAL_SECONDS if reaper_interval is None else reaper_interval
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
			self._cleanup_orphans()
			self._started = True
			self._reaper_thread = threading.Thread(
				target=self._reaper_loop, name="nvdaHttpBridgeDiagnosticsReaper", daemon=True,
			)
			self._reaper_thread.start()

	def create(self):
		self.start()
		self.cleanup_expired()
		self._security_check()
		now = self._monotonic()
		with self._lock:
			if self._closing:
				raise ServiceUnavailable()
			if sum(job.status in ("queued", "running") for job in self._jobs.values()) >= DIAGNOSTICS_MAX_CONCURRENT:
				raise TooManyRequests("A diagnostic export is already running")
			if sum(job.status == "completed" for job in self._jobs.values()) >= DIAGNOSTICS_MAX_RETAINED_JOBS:
				raise TooManyRequests("The retained diagnostic export limit was reached")
			os.makedirs(self._root, exist_ok=True)
			job_id = random_urlsafe(18)
			job = DiagnosticsJob(
				job_id,
				os.path.join(self._root, job_id + ".part"),
				os.path.join(self._root, job_id + ".zip"),
				now,
				now + DIAGNOSTICS_TTL_SECONDS,
				_utc_after(),
				_utc_after(DIAGNOSTICS_TTL_SECONDS),
			)
			self._jobs[job_id] = job
			job.thread = threading.Thread(
				target=self._run, args=(job,), name="nvdaHttpBridgeDiagnosticsExport", daemon=True,
			)
			job.thread.start()
			return job.snapshot(now)

	def _run(self, job):
		try:
			with job.lock:
				if job.cancel_event.is_set():
					job.status = "canceled"
					return
				job.status = "running"
				job.started_at = self._monotonic()

			def collect():
				self._security_check()
				return self._adapter.snapshot()

			snapshot = self._executor.call(collect, 5000)
			if job.cancel_event.is_set():
				raise _Canceled()
			with zipfile.ZipFile(job.part_path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
				archive.writestr(
					"diagnostics.json",
					json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8"),
				)
				log_tail = self._read_log_tail()
				if log_tail is not None:
					archive.writestr("nvda-log-tail.txt", log_tail)
			if os.path.getsize(job.part_path) > DIAGNOSTICS_MAX_BYTES:
				raise Conflict("The diagnostic export exceeded its size limit")
			self._executor.call(self._security_check, 5000)
			with job.lock:
				if job.cancel_event.is_set():
					raise _Canceled()
				os.replace(job.part_path, job.data_path)
				job.byte_count = os.path.getsize(job.data_path)
				job.status = "completed"
				job.finished_at = self._monotonic()
				job.expires_at = job.finished_at + DIAGNOSTICS_TTL_SECONDS
				job.expires_wall = _utc_after(DIAGNOSTICS_TTL_SECONDS)
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
					"code": getattr(error, "code", "diagnosticsExportFailed"),
					"message": getattr(error, "message", "The diagnostic export could not be completed"),
				}
		finally:
			if job.status != "completed":
				self._delete_files(job)

	def _read_log_tail(self):
		if self._log_path_provider is None:
			return None
		try:
			path = self._log_path_provider()
			with open(path, "rb") as source:
				source.seek(0, os.SEEK_END)
				size = source.tell()
				source.seek(max(0, size - DIAGNOSTICS_LOG_TAIL_BYTES))
				return source.read(DIAGNOSTICS_LOG_TAIL_BYTES)
		except (OSError, TypeError):
			return None

	def status(self, job_id):
		self.cleanup_expired()
		return self._get(job_id).snapshot(self._monotonic())

	def open_data(self, job_id):
		self.cleanup_expired()
		job = self._get(job_id)
		with job.lock:
			if job.status != "completed" or job.cancel_event.is_set():
				raise Conflict("The diagnostic export data is not ready")
			try:
				data_file = open(job.data_path, "rb")
			except OSError:
				raise Conflict("The diagnostic export data is not ready")
			return data_file, os.fstat(data_file.fileno()).st_size

	def is_downloadable(self, job_id):
		with self._lock:
			job = self._jobs.get(job_id)
		if job is None:
			return False
		with job.lock:
			return (
				job.status == "completed" and not job.cancel_event.is_set()
				and job.expires_at > self._monotonic() and os.path.isfile(job.data_path)
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
					job.expires_at = self._monotonic()
		self._reaper_wake.set()

	def cleanup_expired(self):
		now = self._monotonic()
		with self._lock:
			expired = [(key, job) for key, job in self._jobs.items()
				if job.status not in ("queued", "running") and job.expires_at <= now]
		for key, job in expired:
			self._delete_files(job)
			with self._lock:
				if self._jobs.get(key) is job:
					self._jobs.pop(key, None)

	def metrics(self):
		self.cleanup_expired()
		with self._lock:
			return {
				"active": sum(job.status in ("queued", "running") for job in self._jobs.values()),
				"retained": sum(job.status == "completed" for job in self._jobs.values()),
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
			if job.thread and job.thread.is_alive() and job.thread is not threading.current_thread():
				job.thread.join(1.0)
			self._delete_files(job)
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
			entries = list(os.scandir(self._root))
		except OSError:
			return
		for entry in entries:
			try:
				if entry.is_file() and entry.name.endswith((".part", ".zip")):
					os.remove(entry.path)
			except OSError:
				pass

	def _get(self, job_id):
		with self._lock:
			job = self._jobs.get(job_id)
		if job is None:
			raise NotFound("Unknown diagnostic export job")
		return job

	@staticmethod
	def _delete_files(job):
		for path in (job.part_path, job.data_path):
			try:
				os.remove(path)
			except OSError:
				pass


class _Canceled(Exception):
	pass
