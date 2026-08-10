"""Recognition by driving a coding-agent CLI already installed on this machine.

A third transport alongside ``recognizer`` (cloud REST) and ``local_engine``
(offline OCR). Instead of calling a vision API with our own key, this shells out
to **Claude Code** (``claude``) or **Codex** (``codex``) and lets the agent read
the page image with its own file-reading tool. The agent carries its own
credentials, so there is no API key to store here -- the user's existing
subscription or login does the work.

Same contract as every other recognizer: hand a page image to a model, get the
handwritten table back as JSON rows, then feed the identical
correction -> assembly -> audit core. The prompt and the JSON parsing are
imported from ``recognizer`` rather than restated, so all three transports stay
in step.

Three things make this different from the REST path, and each shapes the code:

* **Auth is not ours.** Availability is "is the CLI on PATH", not "is a key
  saved", and a login problem surfaces as a non-zero exit rather than a 401.
* **The prompt goes on stdin.** Windows caps a command line at ~8 KB and the
  prompt plus an absolute path is close enough to that to matter.
* **The agent is a harness, not a bare model.** It carries a large system
  prompt, discovers project files, and can run tools. We pin it down: only the
  file-reading tool is allowed, customisations are disabled, and no session is
  written to disk. That keeps the run deterministic, cheap and side-effect free.

Cost is real and worth knowing before a batch: measured on the golden-set page
photographs, one page is roughly $0.2-$0.45 through Claude Code on
``claude-opus-5`` (the agent's own system prompt dominates the input tokens).
Accuracy is the best of any mode here -- see README.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from app.recognizer import PROMPT, RawRow, rows_from_text

#: Seconds to wait for one page. Generous: an agent CLI pays process start-up
#: and model-load latency the REST path does not, and a dense page at high
#: effort is minutes of work. A page measured 20-31s; this is headroom, not a
#: target.
CLI_TIMEOUT = 300

PROVIDERS = ("claudecode", "codex")

#: Appended to the shared PROMPT. The cloud providers attach the image to the
#: request; an agent has to be told which file to open, and told to answer with
#: the JSON only -- left alone it writes a conversational preamble around it.
_READ_INSTRUCTION = """
The page photograph is the image file at:
{path}

Read that exact file, then reply with ONLY the JSON object described above.
Do not create, edit or run anything, and do not explain what you are doing.
"""


@dataclass
class AgentResult:
    """One page as the agent read it. Mirrors ``RecognitionResult``."""

    page: str
    rows: list[RawRow] = field(default_factory=list)
    error: str | None = None
    #: What the run cost, when the CLI reports it. Surfaced to the UI because
    #: this transport bills per page against the user's own account.
    cost_usd: float | None = None


class AgentError(RuntimeError):
    """An agent run failed in a way the user needs to see and act on."""


# ------------------------------------------------------------------ providers


def _claude_argv(executable: str, model: str, read_file: bool = True) -> list[str]:
    """Claude Code in print mode, restricted to reading one file.

    ``--tools Read`` plus ``--allowedTools Read`` is belt-and-braces: the first
    limits which tools exist, the second pre-approves the only one left, so a
    non-interactive run never blocks on a permission prompt it cannot answer.
    ``read_file=False`` drops the tool entirely, for the Settings smoke test
    where there is no image and no reason to grant filesystem access at all.
    """
    argv = [
        executable,
        "-p",
        "--output-format", "json",
        *(["--tools", "Read", "--allowedTools", "Read"] if read_file else ["--tools", ""]),
        # Skip CLAUDE.md, hooks, plugins and skills. Those belong to whatever
        # project the user happens to be in and would otherwise ride along in
        # the prompt, costing tokens and steering the transcription.
        "--safe-mode",
        # Batch of a dozen pages should not leave a dozen resumable sessions.
        "--no-session-persistence",
        # Moves per-machine text out of the system prompt, so the cached prefix
        # is identical across pages -- the later pages of a batch bill as cache
        # reads instead of cache writes.
        "--exclude-dynamic-system-prompt-sections",
    ]
    if model:
        argv += ["--model", model]
    return argv


def _claude_text(stdout: str) -> str:
    """Pull the answer out of ``--output-format json``'s envelope."""
    try:
        payload = json.loads(stdout)
    except ValueError as exc:
        raise AgentError(f"could not parse the CLI's JSON output: {exc}") from exc
    if not isinstance(payload, dict):
        raise AgentError("unexpected CLI output shape")
    if payload.get("is_error"):
        detail = str(payload.get("result") or payload.get("subtype") or "unknown error")
        raise AgentError(f"the agent reported an error: {detail[:300]}")
    result = payload.get("result")
    if not isinstance(result, str) or not result.strip():
        raise AgentError("the agent returned no text")
    return result


def _codex_argv(executable: str, model: str, read_file: bool = True) -> list[str]:
    """Codex in non-interactive exec mode, sandboxed read-only.

    Untested: ``codex`` was not installed on the machine this was written on, so
    this follows the documented CLI interface rather than an observed run. The
    parser below is deliberately forgiving for the same reason. ``--skip-git-
    repo-check`` matters because the upload folder is not a repository. The
    sandbox is already read-only, so ``read_file`` changes nothing here.
    """
    argv = [executable, "exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check"]
    if model:
        argv += ["--model", model]
    argv.append("-")  # read the prompt from stdin
    return argv


def _codex_text(stdout: str) -> str:
    """Recover the final assistant message from Codex's JSONL event stream.

    Scans every line for the last piece of assistant text rather than matching
    one documented event name, and falls back to the raw output. The JSON rows
    parser downstream tolerates surrounding prose, so a slightly wrong slice
    still yields rows -- whereas insisting on an exact event shape would fail
    outright the first time the CLI renamed one.
    """
    texts: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if str(event.get("type", "")).lower() in {"error", "stream_error"}:
            detail = event.get("message") or event.get("error") or "unknown error"
            raise AgentError(f"the agent reported an error: {str(detail)[:300]}")
        found = _harvest_text(event)
        if found:
            texts.append(found)
    if texts:
        return texts[-1]
    if stdout.strip():
        return stdout  # not JSONL after all -- let the rows parser try
    raise AgentError("the agent returned no text")


def _harvest_text(node: object, depth: int = 0) -> str:
    """Deepest-last text payload in an event object, whatever nests it."""
    if depth > 6:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        for key in ("text", "message", "content", "last_agent_message", "output"):
            if key in node:
                found = _harvest_text(node[key], depth + 1)
                if found:
                    return found
        return ""
    if isinstance(node, list):
        for item in reversed(node):
            found = _harvest_text(item, depth + 1)
            if found:
                return found
    return ""


@dataclass(frozen=True)
class _Agent:
    label: str
    executable: str
    build_argv: object       # (executable, model, read_file) -> list[str]
    extract: object          # (stdout) -> str
    install_hint: str
    models: tuple[str, ...]  # suggestions only; the field is free text


_AGENTS: dict[str, _Agent] = {
    "claudecode": _Agent(
        label="Claude Code",
        executable="claude",
        build_argv=_claude_argv,
        extract=_claude_text,
        install_hint=(
            "Install Claude Code and sign in (`claude` must run from a terminal), "
            "then retry: https://claude.com/claude-code"
        ),
        models=("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5", "opus", "sonnet"),
    ),
    "codex": _Agent(
        label="Codex",
        executable="codex",
        build_argv=_codex_argv,
        extract=_codex_text,
        install_hint=(
            "Install the Codex CLI and sign in (`codex` must run from a terminal), "
            "then retry: https://developers.openai.com/codex/cli"
        ),
        models=("gpt-5-codex", "gpt-5", "o4-mini"),
    ),
}


# ------------------------------------------------------------------- helpers


def known_providers() -> list[str]:
    return list(PROVIDERS)


def label_for(provider: str) -> str:
    agent = _AGENTS.get(provider)
    return agent.label if agent else provider


def models_for(provider: str) -> list[str]:
    agent = _AGENTS.get(provider)
    return list(agent.models) if agent else []


def resolve(provider: str) -> str | None:
    """Absolute path to the provider's CLI, or None if it is not on PATH."""
    agent = _AGENTS.get(provider)
    if agent is None:
        return None
    return shutil.which(agent.executable)


def availability(provider: str) -> tuple[bool, str]:
    """Can this provider run here? Answered without launching anything.

    Checked up front so the UI can grey the mode out with a reason, rather than
    letting the user upload pages, press the button and wait for a failure --
    the same contract ``local_engine.availability`` follows.
    """
    agent = _AGENTS.get(provider)
    if agent is None:
        return False, f"Unknown agent provider {provider!r}."
    if resolve(provider) is None:
        return False, f"The `{agent.executable}` command was not found on PATH. {agent.install_hint}"
    return True, ""


def _launch(argv: list[str], prompt: str, cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    """Run the CLI with the prompt on stdin. Raises AgentError on failure.

    ``cwd`` is the folder holding the image: the agent needs read access there,
    and pointing it at a folder with no project files of its own keeps the run
    from picking up context that has nothing to do with a survey page.
    """
    # npm-installed CLIs land as a .cmd shim on Windows, which CreateProcess
    # cannot execute directly -- those have to go through the interpreter.
    if os.name == "nt" and argv[0].lower().endswith((".cmd", ".bat")):
        argv = [os.environ.get("COMSPEC", "cmd.exe"), "/c", *argv]
    try:
        return subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd),
        )
    except subprocess.TimeoutExpired:
        raise AgentError(f"the agent did not finish within {timeout}s") from None
    except OSError as exc:
        raise AgentError(f"could not start the agent: {exc}") from exc


def _reported_cost(stdout: str) -> float | None:
    """Per-run cost, when the CLI volunteers one. Best effort by design."""
    try:
        payload = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("total_cost_usd", "cost_usd"):
        value = payload.get(key)
        if isinstance(value, (int, float)):
            return round(float(value), 4)
    return None


def _failure(result: subprocess.CompletedProcess, label: str) -> str:
    """Turn a non-zero exit into something a user can act on."""
    detail = (result.stderr or result.stdout or "").strip()
    lowered = detail.lower()
    if any(word in lowered for word in ("not logged in", "unauthorized", "authentication", "login")):
        return f"{label} is not signed in. Run `{label.split()[0].lower()}` in a terminal and log in, then retry."
    if "model" in lowered and ("not found" in lowered or "unknown" in lowered):
        return f"{label} did not recognise that model name. Check it in Settings. {detail[:200]}"
    return f"{label} exited with code {result.returncode}. {detail[:300]}" if detail else (
        f"{label} exited with code {result.returncode} and said nothing."
    )


# -------------------------------------------------------------------- public


def recognise_page_agent(
    path: Path | str,
    provider: str = "claudecode",
    model: str = "",
    timeout: int = CLI_TIMEOUT,
) -> AgentResult:
    """Recognise one page by driving an agent CLI. Never raises."""
    page = Path(path).stem
    agent = _AGENTS.get(provider)
    if agent is None:
        return AgentResult(page=page, error=f"unknown agent provider {provider!r}")

    executable = resolve(provider)
    if executable is None:
        return AgentResult(page=page, error=availability(provider)[1])

    image = Path(path).resolve()
    if not image.exists():
        return AgentResult(page=page, error="image file is missing")

    # Forward slashes read the same to every shell and to the agent's own tool,
    # and sidestep backslash escaping inside the prompt text.
    prompt = PROMPT + _READ_INSTRUCTION.format(path=image.as_posix())
    argv = agent.build_argv(executable, model.strip(), True)

    try:
        completed = _launch(argv, prompt, image.parent, timeout)
        if completed.returncode != 0:
            return AgentResult(page=page, error=_failure(completed, agent.label))
        rows = rows_from_text(agent.extract(completed.stdout))
    except AgentError as exc:
        return AgentResult(page=page, error=f"{agent.label}: {exc}")
    except ValueError as exc:  # malformed JSON from rows_from_text
        return AgentResult(page=page, error=f"{agent.label} returned unusable JSON: {exc}")

    return AgentResult(page=page, rows=rows, cost_usd=_reported_cost(completed.stdout))


def test_agent(provider: str = "claudecode", model: str = "") -> tuple[bool, str]:
    """Confirm the CLI runs, is signed in, and accepts the model name.

    The Settings counterpart to ``recognizer.test_key`` -- there is no key to
    check here, so this exercises the thing that actually breaks: login state
    and a model name the provider no longer serves.
    """
    agent = _AGENTS.get(provider)
    if agent is None:
        return False, f"Unknown agent provider {provider!r}."
    ok, reason = availability(provider)
    if not ok:
        return False, reason

    executable = resolve(provider) or agent.executable
    # No image to read in a smoke test, so no filesystem access is granted.
    argv = agent.build_argv(executable, model.strip(), False)

    try:
        completed = _launch(argv, "Reply with the single word: ok", Path.cwd(), 120)
    except AgentError as exc:
        return False, str(exc)

    if completed.returncode != 0:
        return False, _failure(completed, agent.label)
    try:
        text = agent.extract(completed.stdout)
    except AgentError as exc:
        return False, f"{agent.label} ran but its output could not be read: {exc}"

    named = model.strip() or "its default model"
    return True, f"{agent.label} works with {named}. Replied: {text.strip()[:60]}"
