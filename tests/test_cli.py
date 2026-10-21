"""cli.py: what the operator sees in the terminal when the model side fails.

Offline throughout: --offline means the fake model and the console notifier,
and the one call that would reach the model is replaced by an outage."""

from __future__ import annotations

import pytest

from sponsy import cli, prospecting
from sponsy.job import SponsorshipJob
from sponsy.llm import LLMError


@pytest.mark.parametrize("command", ["run", "discover"])
def test_a_model_outage_stops_with_a_message_not_a_traceback(command, tmp_path,
                                                            monkeypatch, capsys):
    """A 503 "high demand" that outlasts the retries used to escape as a
    traceback. The job has already marked the run failed and told Telegram;
    the terminal should say so in one line and exit non-zero."""
    def outage(*args, **kwargs):
        raise LLMError("gemini failed after 6 attempts: HTTP 503: high demand")

    monkeypatch.setattr(SponsorshipJob, "run", outage)
    monkeypatch.setattr(prospecting, "discover", outage)
    code = cli.main(["--offline", "--db", str(tmp_path / "t.db"), command])
    assert code == 2
    assert "stopped: gemini failed after 6 attempts" in capsys.readouterr().out
