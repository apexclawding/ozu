import tempfile
import unittest
from pathlib import Path

from whisper_notes.config import Config, ConfigError, load_config


def _write(content: str) -> Path:
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".toml", delete=False, encoding="utf-8"
    )
    f.write(content)
    f.close()
    return Path(f.name)


class TestLoadConfig(unittest.TestCase):
    def test_missing_file_yields_defaults(self):
        config = load_config(Path("/nonexistent/config.toml"))
        self.assertEqual(config, Config())

    def test_repo_config_toml_parses(self):
        config = load_config(Path(__file__).parent.parent / "config.toml")
        self.assertEqual(config.port, 8420)
        self.assertEqual(config.model_repo, "mlx-community/whisper-large-v3-turbo")

    def test_partial_file_keeps_other_defaults(self):
        config = load_config(_write("[server]\nport = 9000\n"))
        self.assertEqual(config.port, 9000)
        self.assertEqual(config.paragraph_max_chars, 750)

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(_write("[server]\nhost = '0.0.0.0'\n"))

    def test_wrong_type_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(_write("[server]\nport = 'eight'\n"))

    def test_int_accepted_for_float_setting(self):
        config = load_config(_write("[output]\nparagraph_pause_seconds = 3\n"))
        self.assertEqual(config.paragraph_pause_seconds, 3.0)

    def test_out_of_range_port_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(_write("[server]\nport = 99999\n"))

    def test_invalid_toml_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(_write("not [valid toml"))

    def test_derived_properties(self):
        config = Config()
        self.assertEqual(config.max_file_bytes, 2 * 1024**3)
        self.assertEqual(config.max_duration_seconds, 6 * 3600)
        self.assertEqual(config.db_path, Path("data/jobs.db"))


if __name__ == "__main__":
    unittest.main()
