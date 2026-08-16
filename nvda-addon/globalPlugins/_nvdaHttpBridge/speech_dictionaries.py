"""Speech dictionary resources backed by NVDA's SpeechDict objects."""

from .errors import NotFound, PartialFailure, ValidationError
from .resource_utils import mutation_result, reject_unknown, require_object, require_revision, revision


DICTIONARY_IDS = ("default", "voice", "temp")
ENTRY_TYPE_VALUES = {
	"anywhere": 0,
	"regexp": 1,
	"word": 2,
	"partOfWord": 3,
	"startOfWord": 4,
	"endOfWord": 5,
	"unix": 6,
}


class NvdaSpeechDictionaryBackend:
	def _parts(self):
		import speechDictHandler

		try:
			from speechDictHandler.types import DictionaryType, EntryType, SpeechDictEntry

			return speechDictHandler, DictionaryType, EntryType, SpeechDictEntry
		except ImportError:
			return speechDictHandler, None, None, speechDictHandler.SpeechDictEntry

	def resolve(self, dictionary_id):
		handler, dictionary_type, entry_type, entry_cls = self._parts()
		if dictionary_id not in DICTIONARY_IDS:
			raise NotFound("Unknown speech dictionary")
		if dictionary_type is not None:
			definition = handler.definitions._getDictionaryDefinition(dictionary_type(dictionary_id))
			return definition.dictionary, definition.displayName or dictionary_id, entry_type, entry_cls
		try:
			dictionary = handler.dictionaries[dictionary_id]
		except KeyError:
			raise NotFound("The speech dictionary is unavailable")
		return dictionary, dictionary_id, entry_type, entry_cls

	def supported_types(self, dictionary_id):
		unused_dictionary, unused_name, entry_type, unused_cls = self.resolve(dictionary_id)
		if entry_type is None:
			return {name: value for name, value in ENTRY_TYPE_VALUES.items() if value <= 2}
		available = {int(member) for member in entry_type}
		return {name: value for name, value in ENTRY_TYPE_VALUES.items() if value in available}

	def make_entry(self, dictionary_id, raw):
		unused_dictionary, unused_name, entry_type, entry_cls = self.resolve(dictionary_id)
		entry_value = self.supported_types(dictionary_id)[raw["type"]]
		entry_value = entry_type(entry_value) if entry_type is not None else entry_value
		entry = entry_cls(
			raw["pattern"],
			raw["replacement"],
			raw["comment"],
			caseSensitive=raw["caseSensitive"],
			type=entry_value,
		)
		# This also validates replacement group references for regular expressions.
		entry.sub("test")
		return entry

	def should_write(self):
		try:
			from NVDAState import shouldWriteToDisk
		except ImportError:
			return True
		return bool(shouldWriteToDisk())


class SpeechDictionariesAdapter:
	def __init__(self, backend=None):
		self.backend = backend or NvdaSpeechDictionaryBackend()

	@staticmethod
	def _entry_dict(entry):
		type_value = int(entry.type)
		type_name = next((name for name, value in ENTRY_TYPE_VALUES.items() if value == type_value), str(type_value))
		return {
			"pattern": entry.pattern,
			"replacement": entry.replacement,
			"comment": entry.comment,
			"caseSensitive": bool(entry.caseSensitive),
			"type": type_name,
		}

	def list(self):
		items = []
		for dictionary_id in DICTIONARY_IDS:
			try:
				state = self.get(dictionary_id)
			except (KeyError, NotFound):
				continue
			items.append({
				"id": dictionary_id,
				"displayName": state["displayName"],
				"entryCount": len(state["entries"]),
				"persisted": state["scope"]["persistent"],
				"revision": state["revision"],
			})
		return {"items": items, "clearAllSupported": False}

	def get(self, dictionary_id):
		dictionary, display_name, unused_type, unused_cls = self.backend.resolve(dictionary_id)
		entries = [self._entry_dict(entry) for entry in dictionary]
		scope = {"dictionaryId": dictionary_id, "persistent": dictionary_id != "temp"}
		state = {
			"resource": "speech-dictionaries/%s" % dictionary_id,
			"id": dictionary_id,
			"displayName": display_name,
			"scope": scope,
			"entries": entries,
			"entryTypes": list(self.backend.supported_types(dictionary_id)),
			"allowedOperations": ["add", "edit", "remove"],
			"clearAllSupported": False,
		}
		state["revision"] = revision({"scope": scope, "entries": entries})
		return state

	def _validate_entries(self, dictionary_id, entries):
		if not isinstance(entries, list):
			raise ValidationError("entries must be an array")
		if len(entries) > 10000:
			raise ValidationError("entries exceeds the 10000 item limit")
		supported = self.backend.supported_types(dictionary_id)
		built = []
		for index, raw in enumerate(entries):
			if not isinstance(raw, dict):
				raise ValidationError("Each dictionary entry must be an object", details={"index": index})
			reject_unknown(raw, {"pattern", "replacement", "comment", "caseSensitive", "type"}, "entry fields")
			normalized = dict(raw)
			for name in ("pattern", "replacement"):
				if not isinstance(normalized.get(name), str):
					raise ValidationError("%s must be a string" % name, details={"index": index})
			if not normalized["pattern"]:
				raise ValidationError("pattern must not be empty", details={"index": index})
			normalized.setdefault("comment", "")
			normalized.setdefault("caseSensitive", True)
			normalized.setdefault("type", "anywhere")
			if not isinstance(normalized["comment"], str):
				raise ValidationError("comment must be a string", details={"index": index})
			if type(normalized["caseSensitive"]) is not bool:
				raise ValidationError("caseSensitive must be a boolean", details={"index": index})
			if normalized["type"] not in supported:
				raise ValidationError("Unsupported dictionary entry type", details={"index": index, "type": normalized["type"]})
			try:
				built.append(self.backend.make_entry(dictionary_id, normalized))
			except Exception as error:
				raise ValidationError("Invalid speech dictionary entry", details={"index": index, "reason": str(error)})
		return built

	def validate(self, dictionary_id, body):
		body = require_object(body)
		reject_unknown(body, {"entries"})
		built = self._validate_entries(dictionary_id, body.get("entries"))
		return {"status": "ok", "valid": True, "entryCount": len(built), "warnings": []}

	def put(self, dictionary_id, body):
		body = require_object(body)
		reject_unknown(body, {"baseRevision", "entries"})
		before = self.get(dictionary_id)
		require_revision(body, before["revision"])
		built = self._validate_entries(dictionary_id, body.get("entries"))
		if before["entries"] and not built:
			raise ValidationError("Clearing an entire speech dictionary requires confirmation and is not supported")
		new_entries = [self._entry_dict(entry) for entry in built]
		changed = new_entries != before["entries"]
		dictionary, unused_name, unused_type, unused_cls = self.backend.resolve(dictionary_id)
		persistent = dictionary_id != "temp"
		can_write = self.backend.should_write()
		warnings = []
		if changed:
			old_objects = list(dictionary)
			dictionary[:] = built
			try:
				if not persistent or can_write:
					dictionary.save()
			except Exception as error:
				dictionary[:] = old_objects
				rollback_error = None
				try:
					if persistent and can_write:
						dictionary.save()
				except Exception as caught:
					rollback_error = str(caught)
				raise PartialFailure(details={
					"reason": str(error),
					"rollbackError": rollback_error,
					"actual": self.get(dictionary_id),
				})
		if persistent and not can_write:
			warnings.append("NVDA is not currently writing configuration to disk")
		after = self.get(dictionary_id)
		return mutation_result(
			"speech-dictionaries/%s" % dictionary_id,
			changed,
			after,
			persistent and can_write,
			warnings=warnings,
		)
