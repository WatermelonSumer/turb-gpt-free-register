import tempfile
import unittest
from pathlib import Path

from core import db


class RuntimeDataDirectoryTests(unittest.TestCase):
    def test_migrates_previous_project_root_runtime_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "old-root"
            data = Path(tmp) / "runtime-data"
            root.mkdir()
            (root / "注册成功的邮箱.json").write_text("[{\"email\": \"a@example.test\"}]\n", encoding="utf-8")
            (root / "accounts").mkdir()
            (root / "accounts" / "batch.json").write_text("{}\n", encoding="utf-8")

            old_project_root = db._PROJECT_ROOT
            old_data_dir = db._DATA_DIR
            old_log_dir = db._LOG_DIR
            try:
                db._PROJECT_ROOT = root
                db._DATA_DIR = data
                db._LOG_DIR = data / "注册日志"
                db._ensure_storage()
            finally:
                db._PROJECT_ROOT = old_project_root
                db._DATA_DIR = old_data_dir
                db._LOG_DIR = old_log_dir

            self.assertEqual(
                (data / "注册成功的邮箱.json").read_text(encoding="utf-8"),
                "[{\"email\": \"a@example.test\"}]\n",
            )
            self.assertEqual((data / "accounts" / "batch.json").read_text(encoding="utf-8"), "{}\n")
            self.assertTrue((data / "注册日志").is_dir())


if __name__ == "__main__":
    unittest.main()
