"""Bounded speech and NVDA event buffers."""

from collections import deque
from datetime import datetime, timezone
import itertools
import threading
import time
import uuid

from .config import EVENT_BUFFER_SIZE, MAX_EVENT_TEXT, SPEECH_BUFFER_SIZE


def _iso_now():
	return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _safe_text(value, limit=MAX_EVENT_TEXT):
	if value is None:
		return ""
	if not isinstance(value, str):
		value = str(value)
	value = value.encode("utf-8", errors="replace").decode("utf-8")
	return value[:limit]


class EventBuffer:
	def __init__(self, maxsize=EVENT_BUFFER_SIZE, wall_clock=None, enabled=True):
		self.instance_id = uuid.uuid4().hex
		self._items = deque(maxlen=maxsize)
		self._ids = itertools.count(1)
		self._condition = threading.Condition()
		self._closed = False
		self._enabled = bool(enabled)
		self._wall_clock = wall_clock or _iso_now

	def append(self, event_type, data):
		with self._condition:
			if self._closed or not self._enabled:
				return None
			item = {
				"id": next(self._ids),
				"instanceId": self.instance_id,
				"type": _safe_text(event_type, 64),
				"time": self._wall_clock(),
				"data": data,
			}
			self._items.append(item)
			self._condition.notify_all()
			return item

	def read_after(self, last_id=0, event_types=None, limit=100):
		with self._condition:
			items = list(self._items)
		if not items:
			return [], False
		oldest_id = items[0]["id"]
		gap = bool(last_id and last_id < oldest_id - 1)
		allowed = set(event_types or ())
		result = [
			item for item in items
			if item["id"] > last_id and (not allowed or item["type"] in allowed)
		]
		return result[: max(1, limit)], gap

	def recovery_cursor(self, last_id=0):
		"""Return a cursor inside the retained window after an overflow.

		This is deliberately independent of event-type filters. Advancing to the
		start of the retained window prevents an SSE client whose filter currently
		matches nothing from repeatedly observing the same gap in a busy loop.
		"""
		with self._condition:
			if not self._items:
				return last_id
			window_start = self._items[0]["id"] - 1
			return window_start if last_id < window_start else last_id

	def is_current(self, instance_id=None):
		with self._condition:
			return (
				not self._closed
				and self._enabled
				and (instance_id is None or instance_id == self.instance_id)
			)

	def wait_after(self, last_id=0, event_types=None, timeout=15.0, limit=100):
		deadline = time.monotonic() + timeout
		with self._condition:
			while not self._closed:
				if not self._enabled:
					# Treat suspension like a stream close. This wakes established
					# SSE clients immediately when Windows becomes restricted.
					return [], False, True
				items, gap = self.read_after(last_id, event_types, limit)
				if items or gap:
					return items, gap, False
				remaining = deadline - time.monotonic()
				if remaining <= 0:
					return [], False, False
				self._condition.wait(remaining)
			return [], False, True

	def clear(self):
		with self._condition:
			self._items.clear()
			self._condition.notify_all()

	def set_enabled(self, enabled, clear=False, reset_instance=False):
		with self._condition:
			self._enabled = bool(enabled)
			if clear:
				self._items.clear()
			if reset_instance:
				self.instance_id = uuid.uuid4().hex
				self._ids = itertools.count(1)
			self._condition.notify_all()

	def close(self):
		with self._condition:
			self._closed = True
			self._enabled = False
			self._items.clear()
			self._condition.notify_all()


class SpeechObserver:
	def __init__(self, event_buffer, max_history=SPEECH_BUFFER_SIZE, capture_allowed=None):
		self._events = event_buffer
		self._history = deque(maxlen=max_history)
		self._lock = threading.Lock()
		self._enabled = True
		self._capture_allowed = capture_allowed or (lambda: True)

	def on_pre_speech(self, speechSequence, symbolLevel=None, priority=None, **kwargs):
		parts = []
		length = 0
		for item in speechSequence or ():
			if not isinstance(item, str):
				continue
			item = _safe_text(item)
			remaining = MAX_EVENT_TEXT - length
			if remaining <= 0:
				break
			parts.append(item[:remaining])
			length += len(parts[-1])
		text = " ".join(part for part in parts if part).strip()
		if not text:
			return
		entry = {"time": _iso_now(), "text": text}
		if priority is not None:
			entry["priority"] = _safe_text(getattr(priority, "name", priority), 64)
		with self._lock:
			if not self._enabled or not self._capture_allowed():
				return
			self._history.append(entry)
			# Keep the observer lock until the event is appended. A concurrent
			# security transition can then clear both stores after this write, or
			# disable them before it, but can never leave a post-clear event behind.
			self._events.append("speech", dict(entry))

	def history(self, last=None):
		with self._lock:
			items = list(self._history)
		if last is not None:
			items = [] if last <= 0 else items[-last:]
		return items

	def clear(self):
		with self._lock:
			self._history.clear()

	def set_enabled(self, enabled, clear=False):
		with self._lock:
			self._enabled = bool(enabled)
			if clear:
				self._history.clear()
