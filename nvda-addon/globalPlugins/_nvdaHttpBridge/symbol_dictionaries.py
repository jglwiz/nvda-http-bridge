"""Symbol pronunciation resources mirroring SpeechSymbolsDialog.onOk."""

import copy
import glob
import os

from .errors import NotFound, PartialFailure, ValidationError
from .resource_utils import mutation_result, reject_unknown, require_object, require_revision, revision


LEVELS = {"none": 0, "some": 100, "most": 200, "all": 300, "character": 1000}
PRESERVES = {"never": 0, "always": 1, "belowLevel": 2}


class NvdaSymbolDictionaryBackend:
	def current_locale(self):
		import speech

		return speech.getCurrentLanguage()

	def available_locales(self):
		import characterProcessing
		import globalVars

		locales = set()
		for definition in getattr(characterProcessing, "_symbolDictionaryDefinitions", ()):
			locales.update(getattr(definition, "availableLocales", {}))
		if not locales:
			pattern = os.path.join(globalVars.appDir, "locale", "*", "symbols.dic")
			locales.update(os.path.basename(os.path.dirname(path)) for path in glob.glob(pattern))
		return sorted(locales)

	def processor(self, locale, fallback=False):
		import characterProcessing

		try:
			return characterProcessing._localeSpeechSymbolProcessors.fetchLocaleData(locale, fallback=fallback)
		except LookupError:
			raise NotFound("The requested symbol locale is unavailable")

	def new_symbol(self, identifier, replacement, level, preserve, display_name):
		import characterProcessing

		return characterProcessing.SpeechSymbol(identifier, None, replacement, level, preserve, display_name)

	def invalidate(self, locale):
		import characterProcessing

		characterProcessing.SpeechSymbolProcessor.localeSymbols.invalidateLocaleData(locale)
		characterProcessing._localeSpeechSymbolProcessors.invalidateLocaleData(locale)

	def should_write(self):
		try:
			from NVDAState import shouldWriteToDisk
		except ImportError:
			return True
		return bool(shouldWriteToDisk())


class SymbolDictionariesAdapter:
	def __init__(self, backend=None):
		self.backend = backend or NvdaSymbolDictionaryBackend()

	@staticmethod
	def _level_name(value):
		return next((name for name, number in LEVELS.items() if int(value) == number), str(int(value)))

	@staticmethod
	def _preserve_name(value):
		return next((name for name, number in PRESERVES.items() if value == number), str(value))

	def get(self, locale):
		use_fallback = locale == "current"
		if locale == "current":
			locale = self.backend.current_locale()
		processor = self.backend.processor(locale, fallback=use_fallback)
		user_ids = set(processor.userSymbols.symbols)
		symbols = []
		for symbol in processor.computedSymbols.values():
			symbols.append({
				"identifier": symbol.identifier,
				"displayName": symbol.displayName,
				"replacement": symbol.replacement,
				"level": self._level_name(symbol.level),
				"preserve": self._preserve_name(symbol.preserve),
				"builtin": bool(processor.isBuiltin(symbol.identifier)),
				"userDefined": symbol.identifier in user_ids,
			})
		scope = {"locale": processor.locale, "currentLocale": self.backend.current_locale()}
		state = {
			"resource": "symbol-dictionaries/%s" % processor.locale,
			"locale": processor.locale,
			"availableLocales": self.backend.available_locales(),
			"scope": scope,
			"symbols": symbols,
			"levels": list(LEVELS),
			"preserveModes": list(PRESERVES),
			"allowedOperations": ["add", "edit", "removeUserOverride"],
		}
		state["revision"] = revision({"scope": scope, "symbols": symbols})
		return state

	def put(self, locale, body):
		body = require_object(body)
		reject_unknown(body, {"baseRevision", "updates", "remove"})
		before = self.get(locale)
		require_revision(body, before["revision"])
		locale = before["locale"]
		updates = body.get("updates", [])
		removals = body.get("remove", [])
		if not isinstance(updates, list) or not isinstance(removals, list):
			raise ValidationError("updates and remove must be arrays")
		if len(updates) + len(removals) > 10000:
			raise ValidationError("Symbol mutation exceeds the 10000 item limit")
		if any(not isinstance(item, str) or not item for item in removals):
			raise ValidationError("remove must contain non-empty symbol identifiers")
		if len(set(removals)) != len(removals):
			raise ValidationError("remove contains duplicate identifiers")
		processor = self.backend.processor(locale)
		user_ids = set(processor.userSymbols.symbols)
		missing = sorted(set(removals) - user_ids)
		if missing:
			raise ValidationError("Only user symbol overrides can be removed", details={"identifiers": missing})
		prepared = []
		seen = set()
		for index, raw in enumerate(updates):
			if not isinstance(raw, dict):
				raise ValidationError("Each symbol update must be an object", details={"index": index})
			reject_unknown(raw, {"identifier", "replacement", "level", "preserve", "displayName"}, "symbol fields")
			identifier = raw.get("identifier")
			if not isinstance(identifier, str) or not identifier:
				raise ValidationError("identifier must be a non-empty string", details={"index": index})
			if identifier in seen or identifier in removals:
				raise ValidationError("A symbol may only appear once", details={"identifier": identifier})
			seen.add(identifier)
			old = processor.computedSymbols.get(identifier)
			replacement = raw.get("replacement", old.replacement if old else "")
			level_name = raw.get("level", self._level_name(old.level) if old else "all")
			preserve_name = raw.get("preserve", self._preserve_name(old.preserve) if old else "never")
			display_name = raw.get("displayName", old.displayName if old else identifier)
			if not isinstance(replacement, str) or not isinstance(display_name, str):
				raise ValidationError("replacement and displayName must be strings", details={"index": index})
			if level_name not in LEVELS or preserve_name not in PRESERVES:
				raise ValidationError("Unsupported symbol level or preserve mode", details={"index": index})
			prepared.append(self.backend.new_symbol(
				identifier, replacement, LEVELS[level_name], PRESERVES[preserve_name], display_name,
			))

		old_user_symbols = copy.deepcopy(processor.userSymbols.symbols)
		changed = bool(prepared or removals)
		can_write = self.backend.should_write()
		if changed:
			for identifier in removals:
				processor.deleteSymbol(processor.userSymbols.symbols[identifier])
			for symbol in prepared:
				processor.updateSymbol(symbol)
			try:
				if can_write:
					processor.userSymbols.save()
			except Exception as error:
				processor.userSymbols.symbols.clear()
				processor.userSymbols.symbols.update(old_user_symbols)
				rollback_error = None
				try:
					if can_write:
						processor.userSymbols.save()
				except Exception as caught:
					rollback_error = str(caught)
				raise PartialFailure(details={
					"reason": str(error),
					"rollbackError": rollback_error,
					"locale": locale,
				})
			self.backend.invalidate(locale)
		warnings = [] if can_write else ["NVDA is not currently writing configuration to disk"]
		after = self.get(locale)
		return mutation_result(
			"symbol-dictionaries/%s" % locale,
			after["revision"] != before["revision"],
			after,
			can_write,
			warnings=warnings,
		)
