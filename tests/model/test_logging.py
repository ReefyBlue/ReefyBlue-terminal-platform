import json
import logging

from terminal_model.logging import configure_json_logging


def test_json_logging_emits_one_object_per_line(capsys):
    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        configure_json_logging(logging.INFO)
        logging.getLogger("t").info("hello", extra={"job_id": "J1"})
        err = capsys.readouterr().err.strip().splitlines()
        rec = json.loads(err[-1])
        assert rec["msg"] == "hello"
        assert rec["level"] == "INFO"
        assert rec["logger"] == "t"
        assert rec["job_id"] == "J1"
        assert "T" in rec["ts"]
    finally:
        root.handlers[:] = saved
