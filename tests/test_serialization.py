import unittest

from support import FakeAdapter, FakeClock, FakeEnum, FakeNode, GLOBAL_PLUGINS

from _nvdaHttpBridge.config import MAX_OBJECT_TEXT
from _nvdaHttpBridge.errors import StaleObject
from _nvdaHttpBridge.serialization import ObjectRegistry, safe_text, serialize_object


class ObjectSerializationTests(unittest.TestCase):
	def setUp(self):
		self.adapter = FakeAdapter()
		self.registry = ObjectRegistry(adapter=self.adapter)

	def test_only_requested_fields_are_read_and_returned(self):
		node = FakeNode("root")
		result = serialize_object(node, ("name", "role"), self.registry, "g1", self.adapter)

		self.assertEqual({("root", "name"), ("root", "role")}, set(self.adapter.reads))
		self.assertEqual("root", result["name"])
		self.assertEqual(
			{"name": "BUTTON", "display": "button", "value": 1},
			result["role"],
		)
		self.assertNotIn("value", result)
		self.assertNotIn("states", result)

	def test_safe_text_uses_object_text_limit_by_default(self):
		value = "x" * (MAX_OBJECT_TEXT + 17)

		result = safe_text(value)

		self.assertEqual(MAX_OBJECT_TEXT, len(result))
		self.assertEqual(value[:MAX_OBJECT_TEXT], result)

	def test_one_property_failure_is_local_and_other_fields_survive(self):
		node = FakeNode("root")
		node.field_errors["description"] = RuntimeError("COM object disappeared")

		result = serialize_object(
			node,
			("name", "description", "className"),
			self.registry,
			"g1",
			self.adapter,
		)

		self.assertEqual("root", result["name"])
		self.assertEqual("FakeWindow", result["className"])
		self.assertIsNone(result["description"])
		self.assertEqual(
			{"code": "propertyUnavailable", "type": "RuntimeError"},
			result["errors"]["description"],
		)

	def test_protected_value_is_not_exposed(self):
		node = FakeNode("password")
		node.states = [FakeEnum("PROTECTED")]

		result = serialize_object(node, ("value",), self.registry, "g1", self.adapter)

		self.assertIsNone(result["value"])
		self.assertTrue(result["protected"])

	def test_protected_window_text_is_masked_and_states_are_read_once(self):
		node = FakeNode("password")
		node.states = [FakeEnum("PROTECTED")]

		result = serialize_object(node, ("windowText",), self.registry, "g1", self.adapter)

		self.assertIsNone(result["windowText"])
		self.assertTrue(result["protected"])
		self.assertEqual(1, self.adapter.reads.count(("password", "states")))


class ObjectRegistryTests(unittest.TestCase):
	def setUp(self):
		self.clock = FakeClock()
		self.adapter = FakeAdapter()
		self.registry = ObjectRegistry(
			adapter=self.adapter,
			ttl=5.0,
			capacity=10,
			monotonic=self.clock,
		)

	def test_resolve_refreshes_ttl_then_expires_object(self):
		node = FakeNode("root")
		object_id = self.registry.register(node, "generation")

		self.clock.advance(4.0)
		self.assertEqual((node, "generation"), self.registry.resolve(object_id))
		self.clock.advance(4.0)
		self.assertEqual((node, "generation"), self.registry.resolve(object_id))
		self.clock.advance(5.0)
		with self.assertRaises(StaleObject):
			self.registry.resolve(object_id)
		self.assertEqual(0, self.registry.size())

	def test_same_identity_is_reused_only_within_generation_and_ttl(self):
		node = FakeNode("root")
		first = self.registry.register(node, "g1")
		self.assertEqual(first, self.registry.register(node, "g1"))
		self.assertNotEqual(first, self.registry.register(node, "g2"))

		self.clock.advance(6.0)
		replacement = self.registry.register(node, "g1")
		self.assertNotEqual(first, replacement)
		self.assertEqual(replacement, self.registry.register(node, "g1"))

	def test_defunct_object_is_removed(self):
		node = FakeNode("root")
		object_id = self.registry.register(node, "g1")
		node.defunct = True

		with self.assertRaises(StaleObject):
			self.registry.resolve(object_id)
		self.assertEqual(0, self.registry.size())


if __name__ == "__main__":
	unittest.main()
