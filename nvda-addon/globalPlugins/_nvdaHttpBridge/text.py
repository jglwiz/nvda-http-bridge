"""Bounded text ranges and stale-safe caret/selection mutations."""

from .config import DEFAULT_TEXT_CHARS, MAX_TEXT_CHARS, MAX_TEXT_OFFSET
from .errors import Conflict, TextPositionUnavailable, ValidationError
from .resource_utils import reject_unknown, require_object, require_revision, revision
from .serialization import safe_text


class NvdaTextBackend:
	def caret_object(self):
		import api

		return api.getCaretObject()

	@staticmethod
	def _constants():
		import textInfos

		return textInfos

	def current_info(self, obj, position):
		constants = self._constants()
		text_position = constants.POSITION_CARET if position == "caret" else constants.POSITION_SELECTION
		try:
			return obj.makeTextInfo(text_position)
		except (NotImplementedError, RuntimeError) as error:
			if position != "selection":
				if isinstance(error, NotImplementedError):
					raise TextPositionUnavailable(details={"position": position}) from error
				raise
			# UIA providers on NVDA 2026.1 can report an unavailable/empty selection
			# as an exception. A selection with no selected text is the collapsed caret.
			try:
				return obj.makeTextInfo(constants.POSITION_CARET)
			except NotImplementedError as caret_error:
				raise TextPositionUnavailable(details={"position": position}) from caret_error

	def object_chunk(self, obj, offset, max_chars):
		constants = self._constants()
		info = obj.makeTextInfo(constants.POSITION_FIRST)
		info.collapse()
		moved = info.move(constants.UNIT_CHARACTER, offset)
		if moved != offset:
			raise ValidationError("offset is beyond the available object text")
		chunk = info.copy()
		consumed = chunk.move(constants.UNIT_CHARACTER, max_chars, endPoint="end")
		text = safe_text(chunk.text, max_chars) or ""
		probe = chunk.copy()
		truncated = probe.move(constants.UNIT_CHARACTER, 1, endPoint="end") == 1
		return text, consumed, truncated

	def signature(self, obj, known_positions=None):
		# Provider bookmarks are deliberately not used: UIA returns a TextInfo copy,
		# offset providers return tuples, and MSHTML returns an opaque native mark.
		# Hash only the bounded mutation address space plus current caret/selection.
		parts = []
		try:
			text, units, truncated = self.object_chunk(obj, 0, MAX_TEXT_OFFSET)
			parts.append({"text": text, "characterUnits": units, "truncated": truncated})
		except Exception as error:
			parts.append({"error": type(error).__name__})
		known_positions = known_positions or {}
		for position in ("caret", "selection"):
			try:
				info = known_positions.get(position)
				if info is None:
					info = self.current_info(obj, position)
				parts.append({
					"position": position,
					"text": safe_text(info.text, MAX_TEXT_CHARS),
					"collapsed": bool(getattr(info, "isCollapsed", False)),
				})
			except Exception as error:
				parts.append({"position": position, "error": type(error).__name__})
		return parts

	def set_range(self, obj, start, end, caret=False):
		constants = self._constants()
		info = obj.makeTextInfo(constants.POSITION_FIRST)
		info.collapse()
		if info.move(constants.UNIT_CHARACTER, start) != start:
			raise ValidationError("start is beyond the available object text")
		if end != start:
			if info.move(constants.UNIT_CHARACTER, end - start, endPoint="end") != end - start:
				raise ValidationError("end is beyond the available object text")
		if caret:
			info.collapse()
			info.updateCaret()
		else:
			info.updateSelection()


class TextAdapter:
	def __init__(self, backend=None):
		self.backend = backend or NvdaTextBackend()

	@staticmethod
	def limits():
		return {
			"defaultChars": DEFAULT_TEXT_CHARS,
			"maxChars": MAX_TEXT_CHARS,
			"maxOffset": MAX_TEXT_OFFSET,
		}

	def _revision(self, obj, object_id, generation, known_positions=None):
		return revision({
			"objectId": object_id,
			"generation": generation,
			"signature": self.backend.signature(obj, known_positions=known_positions),
		})

	def current(self, position, obj, object_id, generation, max_chars):
		info = self.backend.current_info(obj, position)
		raw_value = info.text
		raw_text = "" if raw_value is None else (raw_value if isinstance(raw_value, str) else str(raw_value))
		text = safe_text(raw_text, max_chars)
		return {
			"resource": "text/%s" % position,
			"position": position,
			"objectId": object_id,
			"generation": generation,
			"text": text,
			"collapsed": bool(getattr(info, "isCollapsed", not bool(text))),
			"truncated": len(raw_text) > len(text),
			"revision": self._revision(obj, object_id, generation, known_positions={position: info}),
		}

	def object_text(self, obj, object_id, generation, offset, max_chars):
		text, consumed, truncated = self.backend.object_chunk(obj, offset, max_chars)
		return {
			"resource": "text/object",
			"objectId": object_id,
			"generation": generation,
			"offset": offset,
			"text": text,
			"characterUnits": consumed,
			"nextOffset": offset + consumed if truncated else None,
			"truncated": truncated,
			"revision": self._revision(obj, object_id, generation),
		}

	def set_caret(self, obj, object_id, generation, body):
		body = require_object(body)
		reject_unknown(body, {"objectId", "generation", "baseRevision", "offset"})
		offset = self._integer(body.get("offset"), "offset", 0, MAX_TEXT_OFFSET)
		current = {"revision": self._revision(obj, object_id, generation)}
		require_revision(body, current["revision"])
		self.backend.set_range(obj, offset, offset, caret=True)
		return {"ok": True, "objectId": object_id, "generation": generation, "offset": offset}

	def set_selection(self, obj, object_id, generation, body):
		body = require_object(body)
		reject_unknown(body, {"objectId", "generation", "baseRevision", "start", "end"})
		start = self._integer(body.get("start"), "start", 0, MAX_TEXT_OFFSET)
		end = self._integer(body.get("end"), "end", start, MAX_TEXT_OFFSET)
		current = {"revision": self._revision(obj, object_id, generation)}
		require_revision(body, current["revision"])
		self.backend.set_range(obj, start, end, caret=False)
		return {
			"ok": True,
			"objectId": object_id,
			"generation": generation,
			"start": start,
			"end": end,
		}

	@staticmethod
	def parse_window(params):
		unknown = sorted(set(params or {}) - {"offset", "maxChars"})
		if unknown:
			raise ValidationError("Unknown text query parameters", details={"unknown": unknown})
		offset = TextAdapter._query_integer(params, "offset", 0, 0, MAX_TEXT_OFFSET)
		max_chars = TextAdapter._query_integer(params, "maxChars", DEFAULT_TEXT_CHARS, 1, MAX_TEXT_CHARS)
		return offset, max_chars

	@staticmethod
	def _query_integer(params, name, default, minimum, maximum):
		values = (params or {}).get(name)
		if values is None:
			return default
		if len(values) != 1:
			raise ValidationError("%s must be supplied once" % name)
		try:
			value = int(values[0])
		except (TypeError, ValueError):
			raise ValidationError("%s must be an integer" % name)
		return TextAdapter._integer(value, name, minimum, maximum)

	@staticmethod
	def _integer(value, name, minimum, maximum):
		if type(value) is not int or value < minimum or value > maximum:
			raise ValidationError("%s must be between %s and %s" % (name, minimum, maximum))
		return value
