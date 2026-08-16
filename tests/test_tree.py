from dataclasses import replace
import unittest

from support import FakeAdapter, FakeNode, GLOBAL_PLUGINS, set_children

from _nvdaHttpBridge.serialization import ObjectRegistry
from _nvdaHttpBridge.tree import TreeOptions, collect_tree


class TreeTraversalTests(unittest.TestCase):
	def setUp(self):
		self.adapter = FakeAdapter()
		self.registry = ObjectRegistry(adapter=self.adapter)
		self.base_options = TreeOptions(
			root="focus",
			depth=10,
			max_children=20,
			max_nodes=200,
			timeout_ms=1000,
			include=("name",),
			format="flat",
		)

	def collect(self, root, **changes):
		options = replace(self.base_options, **changes)
		return collect_tree(root, options, self.registry, "generation", self.adapter)

	def test_depth_limit_returns_partial_tree_and_reason(self):
		grandchild = FakeNode("grandchild")
		child = FakeNode("child", children=[grandchild])
		root = FakeNode("root", children=[child])

		result = self.collect(root, depth=1)

		self.assertEqual(["root", "child"], [item["object"]["name"] for item in result["tree"]])
		self.assertEqual(2, result["nodeCount"])
		self.assertTrue(result["truncated"])
		self.assertIn("depthLimit", result["truncationReasons"])

	def test_child_limit_is_per_parent(self):
		root = FakeNode("root", children=[FakeNode("a"), FakeNode("b"), FakeNode("c")])

		result = self.collect(root, max_children=2)

		self.assertEqual(["root", "a", "b"], [item["object"]["name"] for item in result["tree"]])
		self.assertIn("childLimit", result["truncationReasons"])

	def test_global_node_limit_stops_all_remaining_branches(self):
		left = FakeNode("left", children=[FakeNode("left-child")])
		right = FakeNode("right", children=[FakeNode("right-child")])
		root = FakeNode("root", children=[left, right])

		result = self.collect(root, max_nodes=3)

		self.assertEqual(3, result["nodeCount"])
		self.assertEqual(["root", "left", "left-child"], [item["object"]["name"] for item in result["tree"]])
		self.assertIn("nodeLimit", result["truncationReasons"])

	def test_ancestor_cycle_is_detected_without_revisiting_node(self):
		root = FakeNode("root")
		child = FakeNode("child")
		set_children(root, child)
		set_children(child, root)

		result = self.collect(root)

		self.assertEqual(["root", "child"], [item["object"]["name"] for item in result["tree"]])
		self.assertIn("cycleDetected", result["truncationReasons"])

	def test_flat_format_has_parent_references_depth_and_indexes(self):
		root = FakeNode("root", children=[FakeNode("first"), FakeNode("second")])

		result = self.collect(root, format="flat")
		records = result["tree"]

		self.assertIsNone(records[0]["parentId"])
		self.assertEqual(records[0]["id"], records[1]["parentId"])
		self.assertEqual(records[0]["id"], records[2]["parentId"])
		self.assertEqual([0, 1, 1], [record["depth"] for record in records])
		self.assertEqual([0, 0, 1], [record["index"] for record in records])

	def test_nested_format_builds_ordered_children(self):
		first = FakeNode("first", children=[FakeNode("grandchild")])
		root = FakeNode("root", children=[first, FakeNode("second")])

		result = self.collect(root, format="nested")
		tree = result["tree"]

		self.assertEqual("root", tree["name"])
		self.assertEqual(["first", "second"], [node["name"] for node in tree["children"]])
		self.assertEqual("grandchild", tree["children"][0]["children"][0]["name"])

	def test_child_navigation_errors_are_attached_locally(self):
		child = FakeNode("child")
		child.next_sibling_error = RuntimeError("sibling vanished")
		root = FakeNode("root", children=[child])

		result = self.collect(root)
		child_record = result["tree"][1]["object"]

		self.assertEqual("RuntimeError", child_record["errors"]["nextSibling"]["type"])
		self.assertEqual(2, result["nodeCount"])


if __name__ == "__main__":
	unittest.main()
