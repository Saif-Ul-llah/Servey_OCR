"""Driving a coding-agent CLI as a recognizer.

Everything here runs without the CLIs installed: the subprocess call is the one
thing stubbed out, so the argv we build, the stdout we parse and the errors we
report are all exercised for real. That matters most for Codex, which could not
be run at all on the machine this was written on.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from app import agent_cli


@pytest.fixture
def page(tmp_path):
    """A file standing in for a page photograph -- never opened, only located."""
    image = tmp_path / "page_08.jpeg"
    image.write_bytes(b"not really a jpeg")
    return image


def _fake_run(stdout="", stderr="", returncode=0, capture=None):
    """Stub for subprocess.run that records the call it was given."""

    def runner(argv, **kwargs):
        if capture is not None:
            capture["argv"] = argv
            capture.update(kwargs)
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return runner


ROWS_JSON = json.dumps(
    {"rows": [
        {"survey": "645", "meter": "SFT68201", "remarks": ""},
        {"survey": "", "meter": "SFU69314", "remarks": "Shop"},
    ]}
)


class TestProviderTable:
    def test_both_providers_are_registered(self):
        assert set(agent_cli.known_providers()) == {"claudecode", "codex"}

    def test_every_provider_has_a_label_and_model_suggestions(self):
        for provider in agent_cli.known_providers():
            assert agent_cli.label_for(provider)
            assert agent_cli.models_for(provider)

    def test_unknown_provider_is_reported_not_crashed(self):
        ok, reason = agent_cli.availability("copilot")
        assert not ok
        assert "copilot" in reason

    def test_a_missing_cli_explains_how_to_install_it(self, monkeypatch):
        monkeypatch.setattr(agent_cli.shutil, "which", lambda _: None)
        ok, reason = agent_cli.availability("claudecode")
        assert not ok
        assert "not found on PATH" in reason
        assert "http" in reason  # the install hint, not just a bare failure


class TestClaudeArgv:
    def _argv(self, page, monkeypatch, model="claude-opus-5"):
        capture: dict = {}
        monkeypatch.setattr(agent_cli.shutil, "which", lambda _: "/bin/claude")
        monkeypatch.setattr(
            agent_cli.subprocess, "run",
            _fake_run(stdout=json.dumps({"is_error": False, "result": ROWS_JSON}), capture=capture),
        )
        agent_cli.recognise_page_agent(page, "claudecode", model)
        return capture

    def test_runs_in_print_mode_with_json_output(self, page, monkeypatch):
        argv = self._argv(page, monkeypatch)["argv"]
        assert "-p" in argv
        assert argv[argv.index("--output-format") + 1] == "json"

    def test_only_the_read_tool_is_offered_and_pre_approved(self, page, monkeypatch):
        argv = self._argv(page, monkeypatch)["argv"]
        # Both flags matter: one limits the tool set, the other stops a
        # non-interactive run blocking on a permission prompt it cannot answer.
        assert argv[argv.index("--tools") + 1] == "Read"
        assert argv[argv.index("--allowedTools") + 1] == "Read"

    def test_project_customisations_and_sessions_are_disabled(self, page, monkeypatch):
        argv = self._argv(page, monkeypatch)["argv"]
        # A stray CLAUDE.md would otherwise ride along in the prompt and steer
        # the transcription; a batch would also leave a session file per page.
        assert "--safe-mode" in argv
        assert "--no-session-persistence" in argv

    def test_model_is_passed_through_when_set(self, page, monkeypatch):
        argv = self._argv(page, monkeypatch, "claude-sonnet-5")["argv"]
        assert argv[argv.index("--model") + 1] == "claude-sonnet-5"

    def test_blank_model_defers_to_the_cli_configuration(self, page, monkeypatch):
        argv = self._argv(page, monkeypatch, "  ")["argv"]
        assert "--model" not in argv

    def test_prompt_goes_on_stdin_with_a_posix_image_path(self, page, monkeypatch):
        call = self._argv(page, monkeypatch)
        # Forward slashes, so no backslash needs escaping inside the prompt.
        assert page.resolve().as_posix() in call["input"]
        # On stdin, not argv: Windows caps a command line at ~8 KB and the
        # prompt plus an absolute path is close enough to that to matter.
        assert not any(page.name in arg for arg in call["argv"])
        # Run from the image's own folder, so the agent needs no wider access.
        assert call["cwd"] == str(page.parent)


class TestClaudeOutput:
    def test_rows_are_parsed_out_of_the_json_envelope(self, page, monkeypatch):
        monkeypatch.setattr(agent_cli.shutil, "which", lambda _: "/bin/claude")
        monkeypatch.setattr(
            agent_cli.subprocess, "run",
            _fake_run(stdout=json.dumps({"is_error": False, "result": ROWS_JSON})),
        )
        result = agent_cli.recognise_page_agent(page, "claudecode")
        assert result.error is None
        assert [r.meter for r in result.rows] == ["SFT68201", "SFU69314"]
        assert result.rows[1].remarks == "Shop"

    def test_a_fenced_code_block_in_the_answer_is_tolerated(self, page, monkeypatch):
        # The agent habitually wraps JSON in ```json fences.
        fenced = f"```json\n{ROWS_JSON}\n```"
        monkeypatch.setattr(agent_cli.shutil, "which", lambda _: "/bin/claude")
        monkeypatch.setattr(
            agent_cli.subprocess, "run",
            _fake_run(stdout=json.dumps({"is_error": False, "result": fenced})),
        )
        assert len(agent_cli.recognise_page_agent(page, "claudecode").rows) == 2

    def test_reported_cost_is_surfaced(self, page, monkeypatch):
        monkeypatch.setattr(agent_cli.shutil, "which", lambda _: "/bin/claude")
        monkeypatch.setattr(
            agent_cli.subprocess, "run",
            _fake_run(stdout=json.dumps(
                {"is_error": False, "result": ROWS_JSON, "total_cost_usd": 0.071234})),
        )
        # Rounded, because this is shown to a person, not accounted against.
        assert agent_cli.recognise_page_agent(page, "claudecode").cost_usd == 0.0712

    def test_an_agent_reported_error_becomes_a_readable_message(self, page, monkeypatch):
        monkeypatch.setattr(agent_cli.shutil, "which", lambda _: "/bin/claude")
        monkeypatch.setattr(
            agent_cli.subprocess, "run",
            _fake_run(stdout=json.dumps({"is_error": True, "result": "usage limit reached"})),
        )
        result = agent_cli.recognise_page_agent(page, "claudecode")
        assert result.rows == []
        assert "usage limit reached" in result.error

    def test_non_json_stdout_is_reported_rather_than_raised(self, page, monkeypatch):
        monkeypatch.setattr(agent_cli.shutil, "which", lambda _: "/bin/claude")
        monkeypatch.setattr(agent_cli.subprocess, "run", _fake_run(stdout="I cannot do that"))
        result = agent_cli.recognise_page_agent(page, "claudecode")
        assert result.error and "JSON" in result.error


class TestCodexArgv:
    def _argv(self, page, monkeypatch):
        capture: dict = {}
        monkeypatch.setattr(agent_cli.shutil, "which", lambda _: "/bin/codex")
        monkeypatch.setattr(
            agent_cli.subprocess, "run",
            _fake_run(stdout=json.dumps({"type": "item.completed", "text": ROWS_JSON}), capture=capture),
        )
        agent_cli.recognise_page_agent(page, "codex", "gpt-5-codex")
        return capture["argv"]

    def test_runs_exec_sandboxed_read_only(self, page, monkeypatch):
        argv = self._argv(page, monkeypatch)
        assert argv[1] == "exec"
        assert argv[argv.index("--sandbox") + 1] == "read-only"

    def test_git_repo_check_is_skipped(self, page, monkeypatch):
        # The upload folder is not a repository, so the check would refuse.
        assert "--skip-git-repo-check" in self._argv(page, monkeypatch)

    def test_prompt_is_read_from_stdin(self, page, monkeypatch):
        assert self._argv(page, monkeypatch)[-1] == "-"


class TestCodexOutput:
    """Parsed defensively: this CLI could not be run while writing it."""

    def _run(self, page, monkeypatch, stdout):
        monkeypatch.setattr(agent_cli.shutil, "which", lambda _: "/bin/codex")
        monkeypatch.setattr(agent_cli.subprocess, "run", _fake_run(stdout=stdout))
        return agent_cli.recognise_page_agent(page, "codex")

    def test_last_assistant_text_in_a_jsonl_stream_wins(self, page, monkeypatch):
        stream = "\n".join([
            json.dumps({"type": "thread.started"}),
            json.dumps({"type": "item.completed", "text": "let me look at the page"}),
            json.dumps({"type": "item.completed", "text": ROWS_JSON}),
        ])
        result = self._run(page, monkeypatch, stream)
        assert [r.meter for r in result.rows] == ["SFT68201", "SFU69314"]

    def test_text_nested_in_a_content_list_is_found(self, page, monkeypatch):
        stream = json.dumps({"type": "item", "content": [{"type": "output_text", "text": ROWS_JSON}]})
        assert len(self._run(page, monkeypatch, stream).rows) == 2

    def test_plain_stdout_still_parses_when_it_is_not_jsonl(self, page, monkeypatch):
        # If --json ever changes shape, falling back to the whole of stdout
        # keeps the mode working instead of failing on an event-name mismatch.
        assert len(self._run(page, monkeypatch, ROWS_JSON).rows) == 2

    def test_an_error_event_is_surfaced(self, page, monkeypatch):
        stream = json.dumps({"type": "error", "message": "quota exhausted"})
        result = self._run(page, monkeypatch, stream)
        assert result.rows == []
        assert "quota exhausted" in result.error


class TestFailureReporting:
    def test_a_missing_image_is_reported_before_launching_anything(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_cli.shutil, "which", lambda _: "/bin/claude")
        called = []
        monkeypatch.setattr(agent_cli.subprocess, "run", lambda *a, **k: called.append(1))
        result = agent_cli.recognise_page_agent(tmp_path / "nope.jpeg", "claudecode")
        assert result.error == "image file is missing"
        assert not called

    def test_a_login_failure_says_to_sign_in(self, page, monkeypatch):
        monkeypatch.setattr(agent_cli.shutil, "which", lambda _: "/bin/claude")
        monkeypatch.setattr(
            agent_cli.subprocess, "run",
            _fake_run(stderr="Error: not logged in", returncode=1),
        )
        result = agent_cli.recognise_page_agent(page, "claudecode")
        assert "not signed in" in result.error

    def test_an_unknown_model_points_at_settings(self, page, monkeypatch):
        monkeypatch.setattr(agent_cli.shutil, "which", lambda _: "/bin/claude")
        monkeypatch.setattr(
            agent_cli.subprocess, "run",
            _fake_run(stderr="model not found: claude-opus-9", returncode=1),
        )
        assert "Settings" in agent_cli.recognise_page_agent(page, "claudecode").error

    def test_a_timeout_is_reported_with_its_limit(self, page, monkeypatch):
        def boom(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=300)

        monkeypatch.setattr(agent_cli.shutil, "which", lambda _: "/bin/claude")
        monkeypatch.setattr(agent_cli.subprocess, "run", boom)
        result = agent_cli.recognise_page_agent(page, "claudecode", timeout=300)
        assert "300s" in result.error

    def test_a_cli_that_cannot_start_is_reported(self, page, monkeypatch):
        def boom(*args, **kwargs):
            raise OSError("Exec format error")

        monkeypatch.setattr(agent_cli.shutil, "which", lambda _: "/bin/claude")
        monkeypatch.setattr(agent_cli.subprocess, "run", boom)
        assert "could not start" in agent_cli.recognise_page_agent(page, "claudecode").error


class TestSmokeTest:
    def test_the_smoke_test_grants_no_file_access(self, monkeypatch):
        capture: dict = {}
        monkeypatch.setattr(agent_cli.shutil, "which", lambda _: "/bin/claude")
        monkeypatch.setattr(
            agent_cli.subprocess, "run",
            _fake_run(stdout=json.dumps({"is_error": False, "result": "ok"}), capture=capture),
        )
        ok, message = agent_cli.test_agent("claudecode", "claude-opus-5")
        assert ok and "claude-opus-5" in message
        argv = capture["argv"]
        # There is no image to read, so the tool set is emptied rather than
        # handed a filesystem allowance it has no use for.
        assert "Read" not in argv
        assert argv[argv.index("--tools") + 1] == ""

    def test_a_missing_cli_fails_the_test_without_running_it(self, monkeypatch):
        monkeypatch.setattr(agent_cli.shutil, "which", lambda _: None)
        called = []
        monkeypatch.setattr(agent_cli.subprocess, "run", lambda *a, **k: called.append(1))
        ok, message = agent_cli.test_agent("claudecode")
        assert not ok and "not found on PATH" in message
        assert not called

    def test_a_blank_model_is_described_rather_than_left_empty(self, monkeypatch):
        monkeypatch.setattr(agent_cli.shutil, "which", lambda _: "/bin/codex")
        monkeypatch.setattr(agent_cli.subprocess, "run", _fake_run(stdout="ok"))
        ok, message = agent_cli.test_agent("codex", "")
        assert ok and "its default model" in message
