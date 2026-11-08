"""deploy/nas: compose.yml is copied verbatim, so every per-install value comes from .env."""
import re
import unittest
from pathlib import Path

NAS = Path(__file__).resolve().parents[1] / "deploy" / "nas"


class NasComposeTest(unittest.TestCase):
    def test_no_identity_is_written_into_compose(self):
        compose = (NAS / "compose.yml").read_text()
        self.assertNotIn("you@example.com", compose)
        self.assertNotIn("Your Name", compose)
        for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                    "GIT_COMMITTER_EMAIL"):
            self.assertRegex(compose, rf"{key}: \$\{{GIT_(NAME|EMAIL):\?")

    def test_every_variable_compose_reads_is_in_the_readme_env(self):
        compose = (NAS / "compose.yml").read_text()
        wanted = set(re.findall(r"\$\{([A-Z_]+)[:}]", compose))
        env_block = (NAS / "README.md").read_text().split("Create `$DIR/.env`:", 1)[1]
        env_block = env_block.split("```", 2)[1]
        documented = set(re.findall(r"^\s*([A-Z_]+)=", env_block, re.M))
        self.assertEqual(wanted - documented, set())


if __name__ == "__main__":
    unittest.main()
