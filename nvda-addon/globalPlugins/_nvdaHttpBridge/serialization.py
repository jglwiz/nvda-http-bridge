"""Safe, field-selective NVDAObject serialization."""

from collections import OrderedDict
from dataclasses import dataclass
import threading
import time

from .config import ALLOWED_FIELDS, MAX_OBJECT_TEXT, OBJECT_REGISTRY_CAPACITY, OBJECT_TTL_SECONDS
from .errors import StaleObject
from .ids import random_urlsafe


def safe_text(value, limit=MAX_OBJECT_TEXT):
	if value is None:
		return None
	if not isinstance(value, str):
		value = str(value)
	value = value.encode("utf-8", errors="replace").decode("utf-8")
	return value[:limit]


def error_info(error):
	return {"code": "propertyUnavailable", "type": type(error).__name__}


class ObjectAdapter:
	"""Default adapter that only uses the public NVDAObject attribute surface."""

	_attribute_names = {
		"name": "name",
		"role": "role",
		"states": "states",
		"value": "value",
		"description": "description",
		"location": "location",
		"className": "windowClassName",
		"windowText": "windowText",
	}

	def identity(self, obj):
		return id(obj)

	def read_field(self, obj, field):
		if field == "appName":
			app_module = getattr(obj, "appModule")
			return getattr(app_module, "appName", None) if app_module is not None else None
		return getattr(obj, self._attribute_names[field])

	def first_child(self, obj):
		return getattr(obj, "firstChild")

	def next_sibling(self, obj):
		return getattr(obj, "next")

	def is_defunct(self, obj):
		try:
			states = getattr(obj, "states")
		except Exception:
			return False
		return any(getattr(state, "name", "") == "DEFUNCT" for state in states)


@dataclass
class _RegistryEntry:
	obj: object
	generation: str
	expires_at: float


class ObjectRegistry:
	def __init__(self, adapter=None, ttl=OBJECT_TTL_SECONDS, capacity=OBJECT_REGISTRY_CAPACITY, monotonic=None):
		self.adapter = adapter or ObjectAdapter()
		self._ttl = ttl
		self._capacity = capacity
		self._monotonic = monotonic or time.monotonic
		self._entries = OrderedDict()
		self._by_identity = {}
		self._lock = threading.RLock()

	def new_generation(self):
		return random_urlsafe(9)

	def register(self, obj, generation):
		now = self._monotonic()
		try:
			identity = self.adapter.identity(obj)
		except Exception:
			identity = id(obj)
		identity_key = (generation, identity)
		with self._lock:
			existing_id = self._by_identity.get(identity_key)
			if existing_id:
				entry = self._entries.get(existing_id)
				if entry and entry.expires_at > now:
					entry.expires_at = now + self._ttl
					self._entries.move_to_end(existing_id)
					return existing_id
			object_id = "%s.%s" % (generation, random_urlsafe(12))
			self._entries[object_id] = _RegistryEntry(obj, generation, now + self._ttl)
			self._by_identity[identity_key] = object_id
			self._evict(now)
			return object_id

	def _evict(self, now):
		# Entries are moved to the end whenever their common TTL is refreshed,
		# so expired/LRU entries are always at the front. Avoid materializing the
		# whole registry for every exported node (which would make large exports
		# quadratic on NVDA's main thread).
		while self._entries:
			object_id, entry = next(iter(self._entries.items()))
			if entry.expires_at > now and len(self._entries) <= self._capacity:
				break
			self._remove(object_id, entry)

	def _remove(self, object_id, entry):
		self._entries.pop(object_id, None)
		try:
			identity = self.adapter.identity(entry.obj)
		except Exception:
			identity = id(entry.obj)
		identity_key = (entry.generation, identity)
		# A newer entry for the same backend object may already have replaced
		# this index after the old entry expired. Never remove that new mapping
		# while evicting the stale entry.
		if self._by_identity.get(identity_key) == object_id:
			self._by_identity.pop(identity_key, None)

	def resolve(self, object_id, generation=None):
		now = self._monotonic()
		with self._lock:
			entry = self._entries.get(object_id)
			if entry is None or entry.expires_at <= now or (generation and entry.generation != generation):
				if entry is not None:
					self._remove(object_id, entry)
				raise StaleObject()
			if self.adapter.is_defunct(entry.obj):
				self._remove(object_id, entry)
				raise StaleObject()
			entry.expires_at = now + self._ttl
			self._entries.move_to_end(object_id)
			return entry.obj, entry.generation

	def clear(self):
		with self._lock:
			self._entries.clear()
			self._by_identity.clear()

	def size(self):
		with self._lock:
			self._evict(self._monotonic())
			return len(self._entries)


def _enum_value(value):
	name = safe_text(getattr(value, "name", None) or value, 256)
	display = safe_text(getattr(value, "displayString", None) or value, 1024)
	raw = getattr(value, "value", None)
	if not isinstance(raw, (str, int, float, bool, type(None))):
		raw = None
	return {"name": name, "display": display, "value": raw}


def _states_value(states):
	items = [_enum_value(state) for state in states]
	return sorted(items, key=lambda item: item.get("name") or "")


def _location_value(location):
	if location is None:
		return None
	keys = ("left", "top", "width", "height")
	if all(hasattr(location, key) for key in keys):
		return {key: getattr(location, key) for key in keys}
	if isinstance(location, (tuple, list)) and len(location) >= 4:
		return dict(zip(keys, location[:4]))
	return safe_text(location, 1024)


def serialize_object(obj, include, registry, generation, adapter=None):
	adapter = adapter or registry.adapter
	include = tuple(field for field in include if field in ALLOWED_FIELDS)
	object_id = registry.register(obj, generation)
	data = {"objectId": object_id, "generation": generation}
	errors = {}
	protected = False
	field_cache = {}

	# Native window text can expose the same protected edit content as value.
	# Inspect states once and fail closed whenever either sensitive field is
	# requested.
	if "value" in include or "windowText" in include:
		try:
			states_for_protection = adapter.read_field(obj, "states")
			field_cache["states"] = (True, states_for_protection)
			protected = any(getattr(state, "name", "") == "PROTECTED" for state in states_for_protection)
		except Exception as error:
			field_cache["states"] = (False, error)
			protected = True

	for field in include:
		try:
			cached = field_cache.get(field)
			if cached is not None:
				if not cached[0]:
					raise cached[1]
				value = cached[1]
			else:
				value = adapter.read_field(obj, field)
			if field == "role":
				value = _enum_value(value)
			elif field == "states":
				value = _states_value(value)
			elif field == "location":
				value = _location_value(value)
			elif field in ("value", "windowText") and protected:
				value = None
				data["protected"] = True
			elif isinstance(value, str) or value is not None:
				value = safe_text(value)
			data[field] = value
		except Exception as error:
			data[field] = None
			errors[field] = error_info(error)
	if errors:
		data["errors"] = errors
	return data
