"""Input gesture resources backed by NVDA's userGestureMap."""

import copy

from .errors import PartialFailure, ValidationError
from .resource_utils import mutation_result, reject_unknown, require_object, require_revision, revision


class NvdaGesturesBackend:
	def snapshot(self):
		import api
		import inputCore

		obj = api.getFocusObject()
		ancestors = api.getFocusAncestors()
		mappings = inputCore.manager.getAllGestureMappings(obj=obj, ancestors=ancestors)
		app_module = getattr(obj, "appModule", None)
		context = {
			"objectClass": "%s.%s" % (obj.__class__.__module__, obj.__class__.__name__),
			"appName": getattr(app_module, "appName", None),
			"windowHandle": getattr(obj, "windowHandle", None),
			"ancestorClasses": [
				"%s.%s" % (ancestor.__class__.__module__, ancestor.__class__.__name__)
				for ancestor in ancestors
			],
		}
		return mappings, context, inputCore.manager.userGestureMap.export()

	def normalize(self, identifier):
		import inputCore

		if not isinstance(identifier, str) or not identifier or identifier.count(":") != 1:
			raise ValueError("Gesture identifiers must include one source prefix")
		normalized = inputCore.normalizeGestureIdentifier(identifier)
		# Resolving display text verifies that the source and identifier are registered with NVDA.
		inputCore.getDisplayTextForGestureIdentifier(normalized)
		return normalized

	def add(self, gesture, module, class_name, script):
		import inputCore

		inputCore.manager.userGestureMap.add(gesture, module, class_name, script)

	def remove(self, gesture, module, class_name, script):
		import inputCore

		inputCore.manager.userGestureMap.remove(gesture, module, class_name, script)

	def save(self):
		import inputCore

		inputCore.manager.userGestureMap.save()

	def capture_map(self):
		import inputCore

		return copy.deepcopy(inputCore.manager.userGestureMap._map)

	def restore_map(self, saved):
		import inputCore

		inputCore.manager.userGestureMap._map = copy.deepcopy(saved)

	def should_write(self):
		try:
			from NVDAState import shouldWriteToDisk
		except ImportError:
			return True
		return bool(shouldWriteToDisk())


class GesturesAdapter:
	def __init__(self, backend=None):
		self.backend = backend or NvdaGesturesBackend()

	@staticmethod
	def _target(info):
		return {
			"module": info.moduleName,
			"class": info.className,
			"script": info.scriptName,
		}

	def get(self, filter_text=None):
		mappings, context, user_map = self.backend.snapshot()
		commands = []
		needle = (filter_text or "").casefold()
		for category, scripts in mappings.items():
			for display_name, info in scripts.items():
				if needle and needle not in (str(category) + " " + str(display_name)).casefold():
					continue
				target = self._target(info)
				commands.append({
					"id": revision(target)[:16],
					"category": str(category),
					"displayName": str(display_name),
					"target": target,
					"gestures": list(info.gestures),
					"keyboardEmulation": str(info.scriptName).startswith("kb:"),
				})
		commands.sort(key=lambda item: (item["category"].casefold(), item["displayName"].casefold()))
		scope = {"context": "current", "generation": revision(context)}
		state = {
			"resource": "gestures",
			"scope": scope,
			"commands": commands,
			"allowedActions": ["add", "remove", "unbind", "addKbEmulation"],
			"resetAllSupported": False,
		}
		state["revision"] = revision({"context": context, "userMap": user_map})
		return state

	@staticmethod
	def _read_target(raw, index, require_script=True):
		target = raw.get("target")
		if not isinstance(target, dict):
			raise ValidationError("target must be an object", details={"index": index})
		reject_unknown(target, {"module", "class", "script"}, "target fields")
		for name in ("module", "class"):
			if not isinstance(target.get(name), str) or not target[name]:
				raise ValidationError("target.%s must be a non-empty string" % name, details={"index": index})
		if require_script and (not isinstance(target.get("script"), str) or not target["script"]):
			raise ValidationError("target.script must be a non-empty string", details={"index": index})
		return target

	def patch(self, body):
		body = require_object(body)
		reject_unknown(body, {"baseRevision", "operations"})
		before = self.get()
		require_revision(body, before["revision"])
		operations = body.get("operations")
		if not isinstance(operations, list) or not operations:
			raise ValidationError("operations must be a non-empty array")
		if len(operations) > 1000:
			raise ValidationError("operations exceeds the 1000 item limit")
		available_targets = {
			(item["target"]["module"], item["target"]["class"], item["target"]["script"]): set(item["gestures"])
			for item in before["commands"]
		}
		prepared = []
		seen = set()
		for index, raw in enumerate(operations):
			if not isinstance(raw, dict):
				raise ValidationError("Each gesture operation must be an object", details={"index": index})
			action = raw.get("action")
			if action not in before["allowedActions"]:
				raise ValidationError("Unsupported gesture action", details={"index": index, "action": action})
			try:
				gesture = self.backend.normalize(raw.get("gesture"))
			except Exception as error:
				raise ValidationError("Invalid gesture identifier", details={"index": index, "reason": str(error)})
			if action == "addKbEmulation":
				reject_unknown(raw, {"action", "gesture", "targetGesture"}, "operation fields")
				try:
					target_gesture = self.backend.normalize(raw.get("targetGesture"))
				except Exception as error:
					raise ValidationError("Invalid keyboard emulation target", details={"index": index, "reason": str(error)})
				target_source = target_gesture.split(":", 1)[0]
				if target_source != "kb" and not target_source.startswith("kb("):
					raise ValidationError("Keyboard emulation target must use a keyboard source", details={"index": index})
				if gesture == target_gesture:
					raise ValidationError("A gesture cannot emulate itself", details={"index": index})
				target = {"module": "globalCommands", "class": "GlobalCommands", "script": target_gesture}
			else:
				reject_unknown(raw, {"action", "gesture", "target"}, "operation fields")
				target = self._read_target(raw, index)
				tuple_target = (target["module"], target["class"], target["script"])
				if tuple_target not in available_targets:
					raise ValidationError("Target is not available in the current UI context", details={"index": index})
				if action == "add" and gesture in available_targets[tuple_target]:
					raise ValidationError("The gesture is already assigned to this command", details={"index": index})
			signature = (action, gesture, target["module"], target["class"], target["script"])
			if signature in seen:
				raise ValidationError("Duplicate gesture operation", details={"index": index})
			seen.add(signature)
			prepared.append((action, gesture, target))

		old_map = self.backend.capture_map()
		can_write = self.backend.should_write()
		try:
			for action, gesture, target in prepared:
				module, class_name, script = target["module"], target["class"], target["script"]
				if action == "remove":
					try:
						self.backend.remove(gesture, module, class_name, script)
					except ValueError:
						raise ValidationError("The requested user gesture binding does not exist")
				elif action == "unbind":
					try:
						self.backend.remove(gesture, module, class_name, None)
					except ValueError:
						pass
					self.backend.add(gesture, module, class_name, None)
				else:
					try:
						self.backend.remove(gesture, module, class_name, None)
					except ValueError:
						pass
					self.backend.add(gesture, module, class_name, script)
			if can_write:
				self.backend.save()
		except ValidationError:
			self.backend.restore_map(old_map)
			raise
		except Exception as error:
			self.backend.restore_map(old_map)
			rollback_error = None
			try:
				if can_write:
					self.backend.save()
			except Exception as caught:
				rollback_error = str(caught)
			raise PartialFailure(details={"reason": str(error), "rollbackError": rollback_error})
		warnings = [] if can_write else ["NVDA is not currently writing configuration to disk"]
		after = self.get()
		return mutation_result("gestures", after["revision"] != before["revision"], after, can_write, warnings=warnings)
