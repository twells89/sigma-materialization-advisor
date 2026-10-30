import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".githooks" / "check-denylist.sh"
TERM = "ContosoBank"


@unittest.skipUnless(shutil.which("git"), "git is not available")
class DenylistHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "color.ui", "always")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def stage(self, text, name="notes.txt"):
        (self.repo / name).write_text(text)
        self.git("add", name)

    def hook(self, denylist, *args):
        env = {**os.environ, "SPA_DENYLIST": str(denylist)}
        return subprocess.run(["sh", str(HOOK), *(args or ("--staged",))], cwd=self.repo,
                              env=env, capture_output=True, text=True)

    def denylist(self, content):
        path = Path(self.tmp.name) / "deny.txt"
        path.write_bytes(content)
        return path

    def test_staged_banned_term_blocks_even_with_color_always(self):
        self.stage(f"line one\nwe talked to {TERM} today\n")
        proc = self.hook(self.denylist(f"# private list\n{TERM}\n".encode()))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn(TERM.lower(), proc.stderr.lower())

    def test_crlf_denylist_and_padded_terms_still_match(self):
        self.stage(f"{TERM}\n")
        proc = self.hook(self.denylist(f"# c\r\n  {TERM}  \r\n\r\n".encode()))
        self.assertEqual(proc.returncode, 1, proc.stderr)

    def test_added_line_starting_with_plus_plus_is_still_scanned(self):
        self.stage(f"++ {TERM}\n")
        proc = self.hook(self.denylist(f"{TERM}\n".encode()))
        self.assertEqual(proc.returncode, 1, proc.stderr)

    def test_clean_change_passes(self):
        self.stage("nothing private here\n")
        self.assertEqual(self.hook(self.denylist(f"{TERM}\n".encode())).returncode, 0)

    def test_missing_denylist_skips(self):
        self.stage(f"{TERM}\n")
        proc = self.hook(Path(self.tmp.name) / "absent.txt")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("not found", proc.stderr)

    def test_file_mode_checks_a_commit_message(self):
        msg = Path(self.tmp.name) / "msg.txt"
        msg.write_text(f"fix: unblock {TERM}\n")
        proc = self.hook(self.denylist(f"{TERM}\n".encode()), "--file", str(msg))
        self.assertEqual(proc.returncode, 1, proc.stderr)


if __name__ == "__main__":
    unittest.main()
