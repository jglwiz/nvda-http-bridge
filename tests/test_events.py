import unittest

from support import FakeEnum, GLOBAL_PLUGINS

from _nvdaHttpBridge.events import EventBuffer, SpeechObserver


class EventBufferTests(unittest.TestCase):
	def test_buffer_is_bounded_reports_gap_and_filters_types(self):
		buffer = EventBuffer(maxsize=2, wall_clock=lambda: "now")
		buffer.append("focus", {"name": "one"})
		buffer.append("speech", {"text": "two"})
		buffer.append("focus", {"name": "three"})
		buffer.append("speech", {"text": "four"})

		items, gap = buffer.read_after(last_id=1)
		self.assertTrue(gap)
		self.assertEqual([3, 4], [item["id"] for item in items])

		items, gap = buffer.read_after(last_id=1, event_types=("speech",))
		self.assertTrue(gap)
		self.assertEqual([4], [item["id"] for item in items])

	def test_closed_buffer_wakes_as_closed(self):
		buffer = EventBuffer(maxsize=2)
		buffer.close()
		items, gap, closed = buffer.wait_after(timeout=0.01)
		self.assertEqual([], items)
		self.assertFalse(gap)
		self.assertTrue(closed)

	def test_gap_recovery_cursor_advances_when_type_filter_has_no_matches(self):
		buffer = EventBuffer(maxsize=2, wall_clock=lambda: "now")
		for index in range(4):
			buffer.append("focus", {"index": index})

		items, gap, closed = buffer.wait_after(
			last_id=1,
			event_types=("speech",),
			timeout=0.01,
		)
		self.assertEqual([], items)
		self.assertTrue(gap)
		self.assertFalse(closed)

		# IDs 3 and 4 are retained, so 2 is the first cursor that is no
		# longer stale. SSE must adopt it after emitting a reset; otherwise
		# the same wait returns gap immediately forever and spins the server.
		recovered = buffer.recovery_cursor(last_id=1)
		self.assertEqual(2, recovered)
		items, gap = buffer.read_after(
			last_id=recovered,
			event_types=("speech",),
		)
		self.assertEqual([], items)
		self.assertFalse(gap)

	def test_gap_recovery_cursor_never_moves_a_current_client_backwards(self):
		buffer = EventBuffer(maxsize=2, wall_clock=lambda: "now")
		buffer.append("focus", {})
		buffer.append("focus", {})

		self.assertEqual(99, buffer.recovery_cursor(last_id=99))


class SpeechObserverTests(unittest.TestCase):
	def test_speech_keeps_only_text_commands_and_emits_event(self):
		buffer = EventBuffer(maxsize=5, wall_clock=lambda: "event-time")
		observer = SpeechObserver(buffer, max_history=2)

		observer.on_pre_speech(
			["hello", object(), "world"],
			priority=FakeEnum("NORMAL", "normal"),
		)

		history = observer.history()
		self.assertEqual(1, len(history))
		self.assertEqual("hello world", history[0]["text"])
		self.assertEqual("NORMAL", history[0]["priority"])
		items, gap = buffer.read_after()
		self.assertFalse(gap)
		self.assertEqual("speech", items[0]["type"])
		self.assertEqual("hello world", items[0]["data"]["text"])

	def test_speech_history_is_bounded_and_last_zero_is_empty(self):
		observer = SpeechObserver(EventBuffer(), max_history=2)
		observer.on_pre_speech(["one"])
		observer.on_pre_speech(["two"])
		observer.on_pre_speech(["three"])

		self.assertEqual(["two", "three"], [item["text"] for item in observer.history()])
		self.assertEqual(["three"], [item["text"] for item in observer.history(1)])
		self.assertEqual([], observer.history(0))

	def test_non_text_sequence_does_not_create_history_or_event(self):
		buffer = EventBuffer()
		observer = SpeechObserver(buffer)
		observer.on_pre_speech([object(), object()])

		self.assertEqual([], observer.history())
		self.assertEqual(([], False), buffer.read_after())


if __name__ == "__main__":
	unittest.main()
