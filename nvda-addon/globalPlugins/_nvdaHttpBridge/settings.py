"""General settings adapter mirroring GeneralSettingsPanel.onSave."""

from .errors import Conflict, ValidationError
from .resource_utils import mutation_result, reject_unknown, require_object, require_revision, revision


GENERAL_FIELDS = (
	"language",
	"saveConfigurationOnExit",
	"askToExit",
	"playStartAndExitSounds",
	"preventDisplayTurningOff",
)


class NvdaSettingsBackend:
	def values(self):
		import config

		return {name: config.conf["general"][name] for name in GENERAL_FIELDS}

	def languages(self):
		import languageHandler

		return [{"id": code, "displayName": label} for code, label in languageHandler.getAvailableLanguages(True)]

	def language_forced(self):
		import languageHandler

		return bool(languageHandler.isLanguageForced())

	def scope(self):
		import config

		profiles = getattr(config.conf, "profiles", ())
		active = getattr(profiles[-1], "name", None) if profiles else None
		return {"kind": "activeConfiguration", "profile": active}

	def write(self, values):
		import config

		for name, value in values.items():
			config.conf["general"][name] = value


class SettingsAdapter:
	def __init__(self, backend=None):
		self.backend = backend or NvdaSettingsBackend()

	def categories(self):
		return {
			"items": [{
				"id": "general",
				"displayName": "General",
				"endpoint": "/v1/settings/general",
				"writable": True,
			}],
		}

	def get_general(self):
		values = self.backend.values()
		languages = self.backend.languages()
		forced = self.backend.language_forced()
		scope = self.backend.scope()
		state = {
			"resource": "settings/general",
			"scope": scope,
			"values": values,
			"fields": {
				name: {
					"type": "string" if name == "language" else "boolean",
					"writable": not (name == "language" and forced),
					"restartRequired": name == "language",
				}
				for name in GENERAL_FIELDS
			},
			"languageOptions": languages,
			"persistedByThisEndpoint": False,
		}
		state["revision"] = revision({"scope": scope, "values": values})
		return state

	def patch_general(self, body):
		body = require_object(body)
		reject_unknown(body, {"baseRevision", "values"})
		before = self.get_general()
		require_revision(body, before["revision"])
		values = body.get("values")
		if not isinstance(values, dict) or not values:
			raise ValidationError("values must be a non-empty object")
		reject_unknown(values, GENERAL_FIELDS, "General setting fields")
		valid_languages = {item["id"] for item in before["languageOptions"]}
		for name, value in values.items():
			if name == "language":
				if not isinstance(value, str) or value not in valid_languages:
					raise ValidationError("language must be an available NVDA language code")
				if not before["fields"][name]["writable"] and value != before["values"][name]:
					raise Conflict("NVDA language is forced by the command line")
			elif type(value) is not bool:
				raise ValidationError("%s must be a boolean" % name)
		changed = any(before["values"][name] != value for name, value in values.items())
		if changed:
			try:
				self.backend.write(values)
			except Exception:
				self.backend.write(before["values"])
				raise
		after = self.get_general()
		return mutation_result(
			"settings/general",
			changed,
			after,
			False,
			restart_required=("language" in values and values["language"] != before["values"]["language"]),
		)
