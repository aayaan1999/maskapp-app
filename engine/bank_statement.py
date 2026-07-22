import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "bank_statement_config.json"
_bank_statement_config = None


def load_bank_statement_config():
    global _bank_statement_config
    if _bank_statement_config is None:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            _bank_statement_config = json.load(f)
    return _bank_statement_config
