"""NVDA 2025.3.3-compatible adapter for the bridge core."""

import os

from .errors import RestartBlocked, SecureContext, UnsafeAction
from .serialization import ObjectAdapter


class NvdaAdapter(ObjectAdapter):
	def __init__(self, security_state):
		self.security_state = security_state

	@property
	def config_path(self):
		import globalVars

		return globalVars.appArgs.configPath

	@property
	def temp_path(self):
		return os.environ.get("TEMP") or os.environ.get("TMP") or self.config_path

	@property
	def log_path(self):
		import globalVars

		return globalVars.appArgs.logFileName

	def schedule(self, callback):
		import queueHandler

		queueHandler.queueFunction(queueHandler.eventQueue, callback, _immediate=True)

	def get_root(self, root_name):
		import api

		providers = {
			"focus": api.getFocusObject,
			"foreground": api.getForegroundObject,
			"navigator": api.getNavigatorObject,
			"desktop": api.getDesktopObject,
		}
		return providers[root_name]()

	def assert_safe(self, obj=None):
		if self.security_state.restricted():
			raise SecureContext()
		try:
			import globalVars
			from utils.security import isRunningOnSecureDesktop, objectBelowLockScreenAndWindowsIsLocked

			if bool(getattr(globalVars.appArgs, "secure", False)) or isRunningOnSecureDesktop():
				raise SecureContext()
			if obj is not None and objectBelowLockScreenAndWindowsIsLocked(obj):
				raise SecureContext()
		except SecureContext:
			raise
		except Exception:
			# An external bridge must fail closed when it cannot establish safety.
			raise SecureContext("Unable to verify the current Windows security context")

	def initial_security_state(self):
		try:
			import globalVars
			from utils.security import isRunningOnSecureDesktop
			from winAPI.sessionTracking import isLockScreenModeActive

			return {
				"locked": bool(isLockScreenModeActive()),
				"secure_desktop": bool(getattr(globalVars.appArgs, "secure", False) or isRunningOnSecureDesktop()),
				"unknown": False,
			}
		except Exception:
			return {"locked": False, "secure_desktop": False, "unknown": True}

	def version(self):
		import buildVersion

		return {
			"version": str(buildVersion.version),
			"detailed": str(getattr(buildVersion, "version_detailed", buildVersion.version)),
		}

	def nvda_identity(self):
		import globalVars
		import NVDAState

		return {
			"nvdaProcessId": int(globalVars.appPid),
			"nvdaStartTime": float(NVDAState.getStartTime()),
		}

	def assert_restart_allowed(self):
		self.assert_safe()
		from gui.message import isModalMessageBoxActive

		if isModalMessageBoxActive():
			raise RestartBlocked("A modal NVDA message box is active")

	def restart(self):
		import core

		core.restart()

	def speak(self, text):
		import ui

		ui.message(text)

	def cancel_speech(self):
		import speech

		speech.cancelSpeech()

	def execute_gesture(self, key):
		import inputCore
		from keyboardHandler import KeyboardInputGesture

		gesture = KeyboardInputGesture.fromName(key)
		script = gesture.script
		if getattr(script, "__name__", "") in {
			"script_quit",
			"script_restart",
			"script_reloadPlugins",
		}:
			raise UnsafeAction("This gesture resolves to an NVDA lifecycle command")
		inputCore.manager.executeGesture(gesture)

	def focus_object(self, obj):
		obj.setFocus()

	def default_action(self, obj):
		obj.doAction()

	def create_portable_copy(self, destination):
		import installer

		installer.createPortableCopy(destination, shouldCopyUserConfig=True)
