from pathlib import Path

import config

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TEST_CONFIG = config.load(FIXTURES / "console.toml")
