"""Bounded runtime status and explicitly writable NVDA modes."""

from .errors import Conflict, ValidationError
from .resource_utils import mutation_result, reject_unknown, require_object, require_revision, revision


WRITABLE_MODES = ("inputHelp", "sleepMode", "browseMode")


class NvdaStatusBackend:
	def snapshot(self):
		import api
		import braille
		import config
		import inputCore
		import screenCurtain
		from synthDriverHandler import getSynth

		focus = api.getFocusObject()
		app = getattr(focus, "appModule", None)
		interceptor = getattr(focus, "treeInterceptor", None)
		profiles = getattr(config.conf, "profiles", ())
		profile = getattr(profiles[-1], "name", None) if profiles else None
		synth = getSynth()
		display = getattr(getattr(braille, "handler", None), "display", None)
		curtain = getattr(screenCurtain, "screenCurtain", None)
		return {
			"activeProfile": profile,
			"application": {
				"name": getattr(app, "appName", None),
				"processId": getattr(focus, "processID", None),
			},
			"speech": {
				"synthesizer": getattr(synth, "name", None),
				"voice": getattr(synth, "voice", None) if synth is not None else None,
			},
			"braille": {
				"display": getattr(display, "name", None),
				"cells": getattr(display, "numCells", 0) if display is not None else 0,
			},
			"modes": {
				"inputHelp": bool(inputCore.manager.isInputHelpActive),
				"sleepMode": bool(getattr(app, "sleepMode", False)) if app is not None else None,
				"browseMode": (not bool(interceptor.passThrough)) if interceptor is not None else None,
				"screenCurtain": bool(curtain.enabled) if curtain is not None else False,
			},
			"modeAvailability": {
				"inputHelp": True,
				"sleepMode": app is not None,
				"browseMode": interceptor is not None,
				"screenCurtain": curtain is not None,
			},
		}

	def write_modes(self, values):
		import api
		import eventHandler
		import inputCore

		focus = api.getFocusObject()
		app = getattr(focus, "appModule", None)
		interceptor = getattr(focus, "treeInterceptor", None)
		for name, value in values.items():
			if name == "inputHelp":
				inputCore.manager.isInputHelpActive = value
			elif name == "sleepMode":
				if app is None:
					raise Conflict("Sleep mode is unavailable for the current application")
				if bool(app.sleepMode) != value:
					if value:
						eventHandler.executeEvent("loseFocus", focus)
						app.sleepMode = True
					else:
						app.sleepMode = False
						eventHandler.executeEvent("gainFocus", focus)
			elif name == "browseMode":
				if interceptor is None:
					raise Conflict("Browse mode is unavailable for the current object")
				interceptor.passThrough = not value


class StatusAdapter:
	def __init__(self, backend=None):
		self.backend = backend or NvdaStatusBackend()

	def get_status(self):
		state = self.backend.snapshot()
		return {
			"activeProfile": state["activeProfile"],
			"application": state["application"],
			"speech": state["speech"],
			"braille": state["braille"],
			"modes": state["modes"],
		}

	def get_modes(self):
		state = self.backend.snapshot()
		result = {
			"resource": "modes",
			"values": state["modes"],
			"fields": {
				name: {
					"type": "boolean",
					"available": bool(state["modeAvailability"][name]),
					"writable": name in WRITABLE_MODES and bool(state["modeAvailability"][name]),
				}
				for name in state["modes"]
			},
			"scope": {
				"application": state["application"],
				"activeProfile": state["activeProfile"],
			},
		}
		result["revision"] = revision({"values": result["values"], "scope": result["scope"]})
		return result

	def patch_modes(self, body):
		body = require_object(body)
		reject_unknown(body, {"baseRevision", "values"})
		before = self.get_modes()
		require_revision(body, before["revision"])
		values = body.get("values")
		if not isinstance(values, dict) or not values:
			raise ValidationError("values must be a non-empty object")
		reject_unknown(values, WRITABLE_MODES, "Writable mode fields")
		for name, value in values.items():
			if type(value) is not bool:
				raise ValidationError("%s must be a boolean" % name)
			if not before["fields"][name]["writable"]:
				raise Conflict("%s is unavailable in the current context" % name)
		changed = any(before["values"][name] != value for name, value in values.items())
		if changed:
			try:
				self.backend.write_modes(values)
			except Exception:
				rollback = {name: before["values"][name] for name in values}
				self.backend.write_modes(rollback)
				raise
		after = self.get_modes()
		return mutation_result("modes", changed, after, False)
