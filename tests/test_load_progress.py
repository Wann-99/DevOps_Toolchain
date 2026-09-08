"""Loading progress remains isolated across requests and concurrent readers."""

import threading
import unittest
from uuid import uuid4

from ksq.web import load_progress


class LoadProgressTests(unittest.TestCase):
    def test_progress_ownership_success_failure_and_parallel_requests(self) -> None:
        first, second = str(uuid4()), str(uuid4())
        with load_progress.track(first, "admin"):
            load_progress.update("copy", "copying", 1, 4)
            observed = []

            def concurrent_request():
                observed.append(load_progress.snapshot(first, "admin"))
                with load_progress.track(second, "other"):
                    load_progress.update("parse", "parsing")

            thread = threading.Thread(target=concurrent_request)
            thread.start()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(observed[0]["percent"], 25)
            self.assertNotIn("owner", observed[0])
            self.assertEqual(load_progress.snapshot(first, "other")["status"], "waiting")
            self.assertEqual(load_progress.snapshot(first, "admin")["stage"], "copy")
            self.assertEqual(load_progress.snapshot(second, "other")["status"], "done")
        self.assertEqual(load_progress.snapshot(first, "admin")["percent"], 100)
        failed = str(uuid4())
        with self.assertRaisesRegex(ValueError, "bad file"):
            with load_progress.track(failed, "admin"):
                raise ValueError("bad file")
        self.assertEqual(load_progress.snapshot(failed, "admin")["status"], "error")
        for invalid in ("not-a-uuid", first):
            with self.assertRaises(ValueError):
                with load_progress.track(invalid, "admin"):
                    self.fail("Invalid or reused IDs must be rejected")


if __name__ == "__main__":
    unittest.main()
