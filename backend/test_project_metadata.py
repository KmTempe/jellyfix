from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


class ProjectMetadataTests(unittest.TestCase):
    def test_direct_runtime_dependencies_match_requirements(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        declared: dict[str, str] = {}
        for dependency in project["project"]["dependencies"]:
            match = re.fullmatch(r"\s*([A-Za-z0-9_.-]+)==([^\s;]+)\s*", dependency)
            self.assertIsNotNone(match, f"Direct dependency must use an exact version: {dependency}")
            declared[normalized_name(match.group(1))] = match.group(2)

        requirements: dict[str, str] = {}
        for line in (ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)", stripped)
            self.assertIsNotNone(match, f"Runtime requirement must use an exact version: {stripped}")
            requirements[normalized_name(match.group(1))] = match.group(2)

        for name, version in declared.items():
            self.assertEqual(requirements.get(name), version, f"Version mismatch for {name}")
        self.assertNotIn("coverage", requirements)
        self.assertNotIn("jellyfix", requirements)


if __name__ == "__main__":
    unittest.main()
