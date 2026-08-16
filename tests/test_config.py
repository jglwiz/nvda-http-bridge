import unittest

from support import GLOBAL_PLUGINS  # noqa: F401 - installs the product import path

from _nvdaHttpBridge import config
from _nvdaHttpBridge.errors import ExportRequired, ValidationError
from _nvdaHttpBridge.tree import parse_export_options, parse_sync_options


class SyncTreeParameterTests(unittest.TestCase):
	def test_defaults_are_bounded_and_match_public_configuration(self):
		options = parse_sync_options({})

		self.assertEqual("focus", options.root)
		self.assertEqual(config.DEFAULT_TREE_DEPTH, options.depth)
		self.assertEqual(config.DEFAULT_TREE_CHILDREN, options.max_children)
		self.assertEqual(config.DEFAULT_TREE_NODES, options.max_nodes)
		self.assertEqual(config.DEFAULT_TREE_TIMEOUT_MS, options.timeout_ms)
		self.assertEqual(config.DEFAULT_FIELDS, options.include)
		self.assertEqual("nested", options.format)

	def test_explicit_parameters_are_parsed(self):
		options = parse_sync_options(
			{
				"root": ["navigator"],
				"depth": ["4"],
				"maxChildren": ["7"],
				"maxNodes": ["99"],
				"timeoutMs": ["750"],
				"include": ["name,role,name"],
				"format": ["flat"],
			},
		)

		self.assertEqual("navigator", options.root)
		self.assertEqual(4, options.depth)
		self.assertEqual(7, options.max_children)
		self.assertEqual(99, options.max_nodes)
		self.assertEqual(750, options.timeout_ms)
		self.assertEqual(("name", "role"), options.include)
		self.assertEqual("flat", options.format)

	def test_unknown_repeated_and_out_of_range_parameters_are_rejected(self):
		cases = (
			({"unknown": ["1"]}, "Unknown query parameters"),
			({"root": ["focus", "desktop"]}, "must be supplied once"),
			({"depth": ["-1"]}, "at least 0"),
			({"maxNodes": ["0"]}, "at least 1"),
			({"include": ["name,secret"]}, "Unknown object fields"),
			({"include": [""]}, "At least one object field"),
			({"format": ["xml"]}, "Unknown tree format"),
		)
		for params, message in cases:
			with self.subTest(params=params):
				with self.assertRaisesRegex(ValidationError, message):
					parse_sync_options(params)

	def test_values_above_any_sync_hard_limit_require_export(self):
		for parameter, limit in (
			("depth", config.SYNC_MAX_DEPTH),
			("maxChildren", config.SYNC_MAX_CHILDREN),
			("maxNodes", config.SYNC_MAX_NODES),
			("timeoutMs", config.SYNC_MAX_TIMEOUT_MS),
		):
			with self.subTest(parameter=parameter):
				with self.assertRaises(ExportRequired) as caught:
					parse_sync_options({parameter: [str(limit + 1)]})
				self.assertEqual("/v1/tree/exports", caught.exception.details["endpoint"])
				self.assertIn(parameter, caught.exception.details["limits"])


class ExportTreeParameterTests(unittest.TestCase):
	def test_export_accepts_explicit_null_limits_and_field_array(self):
		options = parse_export_options(
			{
				"root": "foreground",
				"depth": None,
				"maxChildren": None,
				"maxNodes": None,
				"include": ["name", "className", "name"],
				"format": "flat",
			}
		)

		self.assertEqual("foreground", options.root)
		self.assertIsNone(options.depth)
		self.assertIsNone(options.max_children)
		self.assertIsNone(options.max_nodes)
		self.assertEqual(("name", "className"), options.include)

	def test_export_body_and_fields_are_strictly_validated(self):
		for body in ([], {"extra": True}, {"root": "missing"}, {"format": "xml"}):
			with self.subTest(body=body):
				with self.assertRaises(ValidationError):
					parse_export_options(body)


if __name__ == "__main__":
	unittest.main()
