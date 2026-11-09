from pathlib import Path

import config
import server

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TEST_CONFIG = config.load(FIXTURES / "console.toml")


def row(number: int, is_train: bool) -> server.PrRow:
    """A PR row of the cache with only the number and the train flag meaningful."""
    return {
        "number": number,
        "title": "",
        "branch": "",
        "scope": "",
        "author": "",
        "head": "",
        "base": "",
        "created_at": "",
        "draft": False,
        "labels": [],
        "review": {"state": "NONE", "author": "", "at": "", "answered": None},
        "is_train": is_train,
        "constituents": [],
        "verify_run": None,
    }
