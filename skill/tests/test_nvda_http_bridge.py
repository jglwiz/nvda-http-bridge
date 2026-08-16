import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "nvda_http_bridge.py"
SPEC = importlib.util.spec_from_file_location("nvda_http_bridge_skill_client", SCRIPT)
nvda_http = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nvda_http)


def health(uptime, status="ok", pid=10, started=100.0):
	return {
		"httpStatus": 200,
		"data": {
			"status": status,
			"uptimeMs": uptime,
			"nvdaProcessId": pid,
			"nvdaStartTime": started,
		},
	}


def capabilities(restart=True):
	data = {"lifecycle": {"restart": {"endpoint": "/v1/lifecycle/restart"}}} if restart else {}
	return {"httpStatus": 200, "data": data}


class FakeClock:
	def __init__(self):
		self.value = 0.0

	def __call__(self):
		return self.value

	def sleep(self, seconds):
		self.value += seconds


class SequenceClient:
	def __init__(self, responses):
		self.responses = iter(responses)
		self.timeout = 8.0

	def json(self, method, path, body=None):
		response = next(self.responses)
		if isinstance(response, Exception):
			raise response
		return response


class ClientTransportTests(unittest.TestCase):
	def test_incomplete_response_is_a_retryable_client_error_and_closes_response(self):
		class InterruptedResponse:
			status = 200
			headers = {"Content-Type": "application/json"}

			def __init__(self):
				self.closed = False

			def read(self):
				raise nvda_http.IncompleteRead(b"", 10)

			def close(self):
				self.closed = True

		response = InterruptedResponse()
		with self.assertRaisesRegex(nvda_http.ClientError, "interrupted"):
			nvda_http.NvdaClient._decode_response(response)
		self.assertTrue(response.closed)


class ConfigurationCommandTests(unittest.TestCase):
	class Client:
		def __init__(self, first=None):
			self.calls = []
			self.first = first

		def json(self, method, path, body=None):
			self.calls.append((method, path, body))
			if len(self.calls) == 1 and self.first is not None:
				return self.first
			return {"httpStatus": 200, "data": {"revision": "actual"}}

	def test_settings_set_reads_structured_body_and_reconciles_running_timeout(self):
		unknown = {
			"httpStatus": 504,
			"data": {"error": {"code": "mainThreadTimeout", "details": {"completionUnknown": True}}},
		}
		with tempfile.TemporaryDirectory() as temporary:
			body_file = Path(temporary) / "settings.json"
			body_file.write_text('{"baseRevision":"old","values":{"askToExit":false}}', encoding="utf-8")
			args = nvda_http.build_parser().parse_args(["settings-set", "--body-file", str(body_file)])
			client = self.Client(unknown)
			result, unused_exit = nvda_http.execute(client, args)
		self.assertEqual("actual", result["reconciliation"]["data"]["revision"])
		self.assertEqual("PATCH", client.calls[0][0])
		self.assertEqual("GET", client.calls[1][0])

	def test_gesture_and_dictionary_commands_use_resource_specific_routes(self):
		client = self.Client()
		nvda_http.execute(client, nvda_http.build_parser().parse_args(["gestures-get", "--filter", "time"]))
		nvda_http.execute(client, nvda_http.build_parser().parse_args(["speech-dictionary-get", "voice"]))
		self.assertEqual("/v1/gestures?context=current&filter=time", client.calls[0][1])
		self.assertEqual("/v1/speech-dictionaries/voice", client.calls[1][1])


class RestartTests(unittest.TestCase):
	def test_restart_uses_http_once_and_requires_new_process_identity(self):
		client = SequenceClient([
			health(10000),
			capabilities(),
			{"httpStatus": 202, "data": {"status": "accepted", "restartId": "r1"}},
			nvda_http.ClientError("bridge unavailable"),
			health(250, pid=11, started=101.0),
		])
		args = nvda_http.build_parser().parse_args(["restart"])
		clock = FakeClock()
		sent = []

		result, exit_code = nvda_http.run_restart(
			client,
			args,
			sender=sent.append,
			clock=clock,
			sleep=clock.sleep,
		)

		self.assertEqual(0, exit_code)
		self.assertEqual([], sent)
		self.assertEqual("restarted", result["data"]["status"])
		self.assertEqual("http", result["data"]["method"])
		self.assertEqual("r1", result["data"]["restartId"])
		self.assertEqual(10000.0, result["data"]["beforeUptimeMs"])
		self.assertEqual(250.0, result["data"]["afterUptimeMs"])
		self.assertTrue(result["data"]["observedUnavailable"])
		self.assertEqual(1.0, client.timeout)

	def test_restart_times_out_when_uptime_does_not_reset(self):
		class StableClient:
			timeout = 8.0

			def json(self, method, path, body=None):
				if path == "/v1/capabilities":
					return capabilities()
				if method == "POST":
					return {"httpStatus": 202, "data": {"restartId": "r1"}}
				return health(100 if method == "GET" else 10000)

		args = nvda_http.build_parser().parse_args([
			"restart",
			"--wait-seconds", "0.2",
			"--poll-interval", "0.1",
		])
		clock = FakeClock()
		result, exit_code = nvda_http.run_restart(
			StableClient(),
			args,
			sender=lambda key: None,
			clock=clock,
			sleep=clock.sleep,
		)

		self.assertEqual(2, exit_code)
		self.assertEqual(408, result["httpStatus"])
		self.assertEqual("restartTimeout", result["data"]["error"]["code"])

	def test_uptime_reset_without_identity_change_is_not_success(self):
		client = SequenceClient([
			health(10000), capabilities(), {"httpStatus": 202, "data": {"restartId": "r1"}},
			health(10), health(20),
		])
		args = nvda_http.build_parser().parse_args(["restart", "--wait-seconds", "0.2", "--poll-interval", "0.1"])
		clock = FakeClock()
		result, exit_code = nvda_http.run_restart(client, args, clock=clock, sleep=clock.sleep)
		self.assertEqual(2, exit_code)
		self.assertEqual("restartTimeout", result["data"]["error"]["code"])

	def test_old_bridge_without_capability_uses_external_hotkey(self):
		client = SequenceClient([
			health(10000), capabilities(False), health(200, pid=11, started=101.0),
		])
		args = nvda_http.build_parser().parse_args(["restart"])
		clock = FakeClock()
		sent = []
		result, exit_code = nvda_http.run_restart(client, args, sender=sent.append, clock=clock, sleep=clock.sleep)
		self.assertEqual(0, exit_code)
		self.assertEqual(["insert"], sent)
		self.assertEqual("externalHotkey", result["data"]["method"])

	def test_declared_endpoint_transport_error_is_not_retried_or_fallback(self):
		client = SequenceClient([
			health(10000), capabilities(), nvda_http.ClientError("incomplete response"),
			health(200, pid=11, started=101.0),
		])
		args = nvda_http.build_parser().parse_args(["restart"])
		clock = FakeClock()
		sent = []
		result, exit_code = nvda_http.run_restart(client, args, sender=sent.append, clock=clock, sleep=clock.sleep)
		self.assertEqual(0, exit_code)
		self.assertEqual([], sent)
		self.assertTrue(result["data"]["postCompletionUnknown"])

	def test_restart_rejects_unhealthy_baseline_before_sending_keys(self):
		client = SequenceClient([health(10000, status="degraded")])
		args = nvda_http.build_parser().parse_args(["restart"])
		sent = []

		with self.assertRaises(nvda_http.ClientError):
			nvda_http.run_restart(client, args, sender=sent.append)

		self.assertEqual([], sent)

	def test_restart_hotkey_is_windows_only(self):
		with patch.object(sys, "platform", "linux"):
			with self.assertRaises(nvda_http.ClientError):
				nvda_http.send_restart_hotkey("insert")


class BackupTests(unittest.TestCase):
	class BackupClient:
		def __init__(self):
			self.calls = []
			self.backup_path = None

		def json(self, method, path, body=None):
			self.calls.append((method, path, body))
			if method == "POST":
				self.backup_path = str(Path(body["targetPath"]) / "nvda")
				output = Path(self.backup_path)
				(output / "userConfig").mkdir(parents=True)
				(output / "nvda.exe").write_bytes(b"portable")
				(output / "userConfig" / "nvda.ini").write_text("config", encoding="utf-8")
				return {"httpStatus": 202, "data": {"jobId": "backup-1", "status": "queued"}}
			if method == "GET":
				return {
					"httpStatus": 200,
					"data": {
						"jobId": "backup-1",
						"status": "completed",
						"fileCount": 2,
						"bytes": 14,
						"backupPath": self.backup_path,
					},
				}
			return {"httpStatus": 202, "data": {"jobId": "backup-1", "status": "canceled"}}

		def download(self, path, output, accept=None):
			raise AssertionError("direct output backups must not be downloaded")

	def test_backup_sends_target_path_and_uses_created_child_folder(self):
		with tempfile.TemporaryDirectory() as temporary:
			root = Path(temporary)
			target = root / "nested-target"
			args = nvda_http.build_parser().parse_args([
				"backup",
				"--output", str(target),
			])
			client = self.BackupClient()

			result, exit_code = nvda_http.run_backup(client, args)

			self.assertEqual(0, exit_code)
			self.assertEqual("backedUp", result["data"]["status"])
			output = target / "nvda"
			self.assertEqual(b"portable", (output / "nvda.exe").read_bytes())
			self.assertEqual("config", (output / "userConfig" / "nvda.ini").read_text(encoding="utf-8"))
			self.assertIn(
				("POST", "/v1/backups", {"targetPath": str(target.resolve())}),
				client.calls,
			)
			self.assertIn(("DELETE", "/v1/backups/backup-1", None), client.calls)

	def test_backup_refuses_existing_destination(self):
		with tempfile.TemporaryDirectory() as temporary:
			root = Path(temporary)
			target = root / "backups"
			(target / "nvda").mkdir(parents=True)
			args = nvda_http.build_parser().parse_args([
				"backup",
				"--output", str(target),
			])

			with self.assertRaises(nvda_http.ClientError):
				nvda_http.run_backup(self.BackupClient(), args)

	def test_backup_defaults_to_nvda_in_current_directory(self):
		args = nvda_http.build_parser().parse_args(["backup"])
		self.assertEqual(Path("."), args.output)

	def test_backup_create_requires_a_target_folder(self):
		class CaptureClient:
			def __init__(self):
				self.calls = []

			def json(self, method, path, body=None):
				self.calls.append((method, path, body))
				return {"httpStatus": 202, "data": {"jobId": "backup-1"}}

		with tempfile.TemporaryDirectory() as temporary:
			target = Path(temporary) / "nested"
			args = nvda_http.build_parser().parse_args([
				"backup-create",
				"--output", str(target),
			])
			client = CaptureClient()

			nvda_http.execute(client, args)

			self.assertEqual(
				[("POST", "/v1/backups", {"targetPath": str(target.resolve())})],
				client.calls,
			)


if __name__ == "__main__":
	unittest.main()
