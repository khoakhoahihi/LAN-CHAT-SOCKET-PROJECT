import os
import subprocess
import sys
import tempfile
import unittest

MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")


class TestMainArguments(unittest.TestCase):
    def run_main(self, *args: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as cwd:
            return subprocess.run(
                [sys.executable, "-X", "utf8", MAIN, "--name", "Zed", *args],
                cwd=cwd, capture_output=True, text=True, timeout=15, input="",
            )

    def test_out_of_range_ports_rejected_without_traceback(self) -> None:
        for flag in ("--port", "--udp-port"):
            for value in ("70000", "-1", "abc"):
                result = self.run_main(flag, value)
                self.assertEqual(result.returncode, 2, (flag, value))
                self.assertNotIn("Traceback", result.stderr)
                self.assertIn("port", result.stderr)


if __name__ == "__main__":
    unittest.main()
