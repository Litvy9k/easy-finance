import importlib.util
import io
from pathlib import Path
import tarfile
import unittest
from unittest.mock import call, patch

spec = importlib.util.spec_from_file_location("release", Path(__file__).resolve().parents[1] / "deploy/ci/ef-ci-deploy.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
REVISION = "a" * 40


def bundle(extra=()):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content in [("app/main.py", b"# app"), ("requirements.txt", b"fastapi"), ("REVISION", (REVISION + "\n").encode()), *extra]:
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/shadow"
                archive.addfile(info)
            else:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


class ReleaseSafetyTests(unittest.TestCase):
    def test_valid_bundle(self):
        self.assertEqual(len(release.validate_archive(bundle(), REVISION)), 3)

    def test_rejects_paths_links_duplicates_and_private_files(self):
        for extra in [("../escape", b"x"), ("/etc/passwd", b"x"), ("app/../../escape", b"x"),
                      ("app\\escape", b"x"), ("app/link", None), (".env", b"secret"),
                      ("easy_finance.db", b"data"), ("app/main.py", b"duplicate"),
                      ("deploy/ci/ef-ci-deploy.py", b"root code")]:
            with self.subTest(extra=extra):
                with self.assertRaises(ValueError):
                    release.validate_archive(bundle([extra]), REVISION)

    def test_rejects_wrong_revision(self):
        for revision in ["main", "a;id", "b" * 40]:
            with self.assertRaises(ValueError):
                release.validate_archive(bundle(), revision)

    def test_rejects_oversized_archive(self):
        with self.assertRaises(ValueError):
            release.validate_archive(b"x" * (release.MAX_ARCHIVE + 1), REVISION)

    def test_failed_health_rolls_back_code_without_restoring_database(self):
        with patch.object(release, "run") as run, patch.object(release, "snapshot_database") as snapshot, \
             patch.object(release, "switch_to") as switch, \
             patch.object(release, "wait_healthy", side_effect=[RuntimeError("bad release"), None]):
            with self.assertRaises(RuntimeError):
                release.activate_release(Path("candidate"), Path("previous"), REVISION)
            self.assertEqual(switch.call_args_list, [call(Path("candidate")), call(Path("previous"))])
            snapshot.assert_called_once_with(REVISION)
            self.assertEqual(run.call_args_list[-1], call(["/usr/bin/systemctl", "start", "easy-finance"], timeout=45))

    def test_snapshot_failure_keeps_original_code_and_restarts_service(self):
        with patch.object(release, "run") as run, \
             patch.object(release, "snapshot_database", side_effect=OSError("full disk")), \
             patch.object(release, "switch_to") as switch, patch.object(release, "wait_healthy"):
            with self.assertRaises(OSError):
                release.activate_release(Path("candidate"), Path("previous"), REVISION)
            switch.assert_not_called()
            self.assertEqual(run.call_args_list[-1], call(["/usr/bin/systemctl", "start", "easy-finance"], timeout=45))


if __name__ == "__main__":
    unittest.main()
