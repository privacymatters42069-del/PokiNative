import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from set_launcher_version import replace_version, set_launcher_version


class LauncherVersionTests(unittest.TestCase):
    def test_replaces_valid_numeric_version_and_rejects_invalid_input(self):
        for version in ("0.4.6", "0.4.6.8"):
            self.assertEqual(
                replace_version("<Version>0.4.6.7</Version>", version),
                f"<Version>{version}</Version>",
            )

        for version in ("0.4", "0.4.6.8.0", "0.4.6.8-test"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                replace_version("<Version>0.4.6.7</Version>", version)

        with self.assertRaises(ValueError):
            replace_version(
                "<Version>0.4.6.7</Version><Version>0.4.6.7</Version>",
                "0.4.6.8",
            )

    def test_sets_both_launcher_projects_without_touching_other_content(self):
        project_paths = (
            Path("launcher/KarPurLauncher.csproj"),
            Path("launcher-linux/KarPurLauncher.Linux.csproj"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in project_paths:
                project = root / path
                project.parent.mkdir(parents=True, exist_ok=True)
                project.write_text(
                    "<Project><Version>0.1.0</Version><Keep>yes</Keep></Project>",
                    encoding="utf-8",
                )

            set_launcher_version("0.4.6.8", root)

            for path in project_paths:
                self.assertEqual(
                    (root / path).read_text(encoding="utf-8"),
                    "<Project><Version>0.4.6.8</Version><Keep>yes</Keep></Project>",
                )


if __name__ == "__main__":
    unittest.main()
