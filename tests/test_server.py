import http.client
import io
import json
import time
import unittest

from support import GLOBAL_PLUGINS  # noqa: F401

from _nvdaHttpBridge.errors import Conflict
from _nvdaHttpBridge.server import BoundedHTTPServer


class FakeService:
	def __init__(self):
		self.health_calls = 0
		self.download_calls = []
		self.download_error = None
		self.download_bytes = b'{"object":{"name":"root"}}\n'
		self.download_handle = None
		self.backup_calls = []
		self.configuration_calls = []
		self.restart_calls = []
		self.server = None

	def health(self):
		self.health_calls += 1
		return {"status": "ok", "source": "fake"}

	def open_export_data(self, job_id):
		self.download_calls.append(job_id)
		if self.download_error is not None:
			raise self.download_error
		self.download_handle = io.BytesIO(self.download_bytes)
		return self.download_handle, len(self.download_bytes)

	def export_download_allowed(self, job_id):
		return True

	def create_backup(self, body):
		self.backup_calls.append(("create", body))
		return {"jobId": "backup-created", "status": "queued"}

	def backup_status(self, job_id):
		self.backup_calls.append(("status", job_id))
		return {"jobId": job_id, "status": "completed"}

	def cancel_backup(self, job_id):
		self.backup_calls.append(("cancel", job_id))
		return {"jobId": job_id, "status": "canceled"}

	def general_settings(self):
		self.configuration_calls.append(("general-get",))
		return {"revision": "g1"}

	def patch_general_settings(self, body):
		self.configuration_calls.append(("general-patch", body))
		return {"revision": "g2"}

	def put_speech_dictionary(self, dictionary_id, body):
		self.configuration_calls.append(("speech-put", dictionary_id, body))
		return {"revision": "d2"}

	def validate_speech_dictionary(self, dictionary_id, body):
		self.configuration_calls.append(("speech-validate", dictionary_id, body))
		return {"valid": True}

	def prepare_restart(self, body):
		self.restart_calls.append(("prepare", body))
		return {
			"status": "accepted",
			"restartId": "restart-1",
			"before": {"nvdaProcessId": 1, "nvdaStartTime": 2.0, "bridgeUptimeMs": 3.0},
		}

	def schedule_prepared_restart(self, restart_id):
		with self.server._active_condition:
			active = self.server._active_requests
		self.restart_calls.append(("scheduled", restart_id, active))
		return True


class BoundedHTTPServerTests(unittest.TestCase):
	def setUp(self):
		self.service = FakeService()
		self.server = BoundedHTTPServer(
			self.service,
			address=("127.0.0.1", 0),
		)
		self.server.start()
		self.service.server = self.server
		self.host, self.port = self.server.server_address

	def tearDown(self):
		self.server.stop()

	def connection(self):
		return http.client.HTTPConnection(self.host, self.port, timeout=2)

	def test_health_uses_real_transport_without_authentication(self):
		connection = self.connection()
		try:
			connection.request("GET", "/health")
			response = connection.getresponse()
			body = json.loads(response.read().decode("utf-8"))
		finally:
			connection.close()

		self.assertEqual(200, response.status)
		self.assertEqual({"status": "ok", "source": "fake"}, body)
		self.assertEqual("no-store", response.getheader("Cache-Control"))
		self.assertEqual(1, self.service.health_calls)

	def test_non_loopback_host_header_is_rejected_before_service_call(self):
		connection = self.connection()
		try:
			connection.putrequest("GET", "/health", skip_host=True)
			connection.putheader("Host", "attacker.example")
			connection.endheaders()
			response = connection.getresponse()
			body = json.loads(response.read().decode("utf-8"))
		finally:
			connection.close()

		self.assertEqual(403, response.status)
		self.assertEqual("forbidden", body["error"]["code"])
		self.assertEqual(0, self.service.health_calls)

	def test_missing_host_header_is_rejected(self):
		connection = self.connection()
		try:
			connection.putrequest("GET", "/health", skip_host=True, skip_accept_encoding=True)
			connection.endheaders()
			response = connection.getresponse()
			body = json.loads(response.read().decode("utf-8"))
		finally:
			connection.close()

		self.assertEqual(403, response.status)
		self.assertEqual("forbidden", body["error"]["code"])
		self.assertEqual(0, self.service.health_calls)

	def test_cross_site_browser_headers_are_rejected_before_service_call(self):
		for headers in (
			{"Origin": "https://attacker.example"},
			{"Sec-Fetch-Site": "cross-site"},
		):
			with self.subTest(headers=headers):
				connection = self.connection()
				try:
					connection.request("GET", "/health", headers=headers)
					response = connection.getresponse()
					body = json.loads(response.read().decode("utf-8"))
				finally:
					connection.close()

				self.assertEqual(403, response.status)
				self.assertEqual("forbidden", body["error"]["code"])
				self.assertEqual(0, self.service.health_calls)

	def test_same_origin_browser_headers_are_allowed(self):
		connection = self.connection()
		try:
			connection.request(
				"GET",
				"/health",
				headers={
					"Origin": "http://127.0.0.1:%s" % self.port,
					"Sec-Fetch-Site": "same-origin",
				},
			)
			response = connection.getresponse()
			body = json.loads(response.read().decode("utf-8"))
		finally:
			connection.close()

		self.assertEqual(200, response.status)
		self.assertEqual({"status": "ok", "source": "fake"}, body)
		self.assertEqual(1, self.service.health_calls)

	def test_export_file_is_opened_before_success_headers_are_sent(self):
		self.service.download_error = Conflict("The export data is not ready")
		connection = self.connection()
		try:
			connection.request("GET", "/v1/tree/exports/job-1/data")
			response = connection.getresponse()
			body = json.loads(response.read().decode("utf-8"))
		finally:
			connection.close()

		self.assertEqual(409, response.status)
		self.assertEqual("application/json; charset=utf-8", response.getheader("Content-Type"))
		self.assertIsNone(response.getheader("Content-Disposition"))
		self.assertEqual("conflict", body["error"]["code"])
		self.assertEqual(["job-1"], self.service.download_calls)

	def test_export_download_uses_open_handle_length_and_closes_it(self):
		connection = self.connection()
		try:
			connection.request("GET", "/v1/tree/exports/job-2/data")
			response = connection.getresponse()
			body = response.read()
		finally:
			connection.close()

		self.assertEqual(200, response.status)
		self.assertEqual(self.service.download_bytes, body)
		self.assertEqual(str(len(body)), response.getheader("Content-Length"))
		self.assertEqual('attachment; filename="job-2.ndjson"', response.getheader("Content-Disposition"))
		self.assertEqual(["job-2"], self.service.download_calls)
		self.assertTrue(self.service.download_handle.closed)

	def test_backup_lifecycle_routes_work_without_authentication(self):
		connection = self.connection()
		try:
			connection.request(
				"POST",
				"/v1/backups",
				body='{"targetPath":"D:/backups"}',
				headers={"Content-Type": "application/json"},
			)
			created_response = connection.getresponse()
			created = json.loads(created_response.read().decode("utf-8"))
			connection.request("GET", "/v1/backups/backup-created")
			status_response = connection.getresponse()
			status_response.read()
			connection.request("DELETE", "/v1/backups/backup-created")
			cancel_response = connection.getresponse()
			cancel_response.read()
		finally:
			connection.close()

		self.assertEqual(202, created_response.status)
		self.assertEqual("backup-created", created["jobId"])
		self.assertEqual(200, status_response.status)
		self.assertEqual(202, cancel_response.status)
		self.assertIn(("create", {"targetPath": "D:/backups"}), self.service.backup_calls)
		self.assertIn(("status", "backup-created"), self.service.backup_calls)
		self.assertIn(("cancel", "backup-created"), self.service.backup_calls)

	def test_restart_response_is_complete_and_request_released_before_callback(self):
		connection = self.connection()
		try:
			connection.request(
				"POST",
				"/v1/lifecycle/restart",
				body="{}",
				headers={"Content-Type": "application/json"},
			)
			response = connection.getresponse()
			payload = json.loads(response.read().decode("utf-8"))
		finally:
			connection.close()
		for _unused in range(100):
			if any(call[0] == "scheduled" for call in self.service.restart_calls):
				break
			time.sleep(0.01)

		self.assertEqual(202, response.status)
		self.assertEqual("close", response.getheader("Connection"))
		self.assertEqual("restart-1", payload["restartId"])
		self.assertEqual([("prepare", {}), ("scheduled", "restart-1", 0)], self.service.restart_calls)

	def test_unknown_routes_return_not_found(self):
		for method, path, body in (
			("GET", "/unknown", None),
			("POST", "/v1/unknown", "{}"),
			("GET", "/v1/backups/job-1/archive", None),
		):
			with self.subTest(method=method, path=path):
				connection = self.connection()
				try:
					connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
					response = connection.getresponse()
					payload = json.loads(response.read().decode("utf-8"))
				finally:
					connection.close()
				self.assertEqual(404, response.status)
				self.assertEqual("notFound", payload["error"]["code"])

	def test_configuration_routes_use_explicit_methods_without_authentication(self):
		connection = self.connection()
		headers = {"Content-Type": "application/json"}
		try:
			connection.request("GET", "/v1/settings/general", headers=headers)
			get_response = connection.getresponse()
			get_response.read()
			connection.request("PATCH", "/v1/settings/general", body='{"baseRevision":"g1","values":{"askToExit":false}}', headers=headers)
			patch_response = connection.getresponse()
			patch_response.read()
			connection.request("POST", "/v1/speech-dictionaries/default/validate", body='{"entries":[]}', headers=headers)
			validate_response = connection.getresponse()
			validate_response.read()
			connection.request("PUT", "/v1/speech-dictionaries/default", body='{"baseRevision":"d1","entries":[]}', headers=headers)
			put_response = connection.getresponse()
			put_response.read()
		finally:
			connection.close()
		self.assertEqual([200, 200, 200, 200], [get_response.status, patch_response.status, validate_response.status, put_response.status])
		self.assertIn(("general-get",), self.service.configuration_calls)
		self.assertIn(("speech-put", "default", {"baseRevision": "d1", "entries": []}), self.service.configuration_calls)


if __name__ == "__main__":
	unittest.main()
