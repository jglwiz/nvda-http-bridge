"""Pure-Python fakes shared by the HTTP bridge unit tests."""

from pathlib import Path
import sys


GLOBAL_PLUGINS = Path(__file__).resolve().parents[1] / "nvda-addon" / "globalPlugins"
if str(GLOBAL_PLUGINS) not in sys.path:
	sys.path.insert(0, str(GLOBAL_PLUGINS))


class FakeClock:
	def __init__(self, value=0.0):
		self.value = float(value)

	def __call__(self):
		return self.value

	def advance(self, seconds):
		self.value += seconds


class FakeEnum:
	def __init__(self, name, display=None, value=None):
		self.name = name
		self.displayString = display if display is not None else name.title()
		self.value = value

	def __str__(self):
		return self.displayString


class FakeAppModule:
	def __init__(self, app_name="fakeApp"):
		self.appName = app_name


class FakeNode:
	def __init__(self, name, *, children=None, identity=None):
		self.name = name
		self.role = FakeEnum("BUTTON", "button", 1)
		self.states = [FakeEnum("FOCUSABLE", "focusable", 2)]
		self.value = "value:" + name
		self.description = "description:" + name
		self.location = (1, 2, 3, 4)
		self.windowClassName = "FakeWindow"
		self.windowText = "window:" + name
		self.appModule = FakeAppModule()
		self.children = []
		self.next = None
		self.identity = identity if identity is not None else id(self)
		self.field_errors = {}
		self.first_child_error = None
		self.next_sibling_error = None
		self.defunct = False
		if children:
			set_children(self, *children)


def set_children(parent, *children):
	parent.children = list(children)
	for index, child in enumerate(parent.children):
		child.next = parent.children[index + 1] if index + 1 < len(parent.children) else None
	return parent


class FakeAdapter:
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

	def __init__(self):
		self.reads = []

	def identity(self, obj):
		return obj.identity

	def read_field(self, obj, field):
		self.reads.append((obj.name, field))
		if field in obj.field_errors:
			raise obj.field_errors[field]
		if field == "appName":
			return obj.appModule.appName
		return getattr(obj, self._attribute_names[field])

	def first_child(self, obj):
		if obj.first_child_error is not None:
			raise obj.first_child_error
		return obj.children[0] if obj.children else None

	def next_sibling(self, obj):
		if obj.next_sibling_error is not None:
			raise obj.next_sibling_error
		return obj.next

	def is_defunct(self, obj):
		return obj.defunct
