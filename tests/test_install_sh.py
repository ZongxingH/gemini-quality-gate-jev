#!/usr/bin/env python3
"""Installer tests: gate selection parsing, the interactive picker, and the
end-to-end --dry-run flow.

Two kinds of coverage:

* unit: ``bash -c 'source install.sh; ...'`` exercises the gate-selection
  functions with a stubbed prompt (no terminal needed);
* integration: the installer itself runs with --dry-run against this checkout,
  so nothing is written and no Gemini CLI state changes.

    python3 tests/test_install_sh.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
ALL_GATES = ["AfterAgent", "BeforeTool", "BeforeAgent", "SessionStart"]

# Sourced by the unit tests: defines the functions without running main().
UNIT_PREAMBLE = """
set -euo pipefail
source "$INSTALL_SH"
"""


def run_bash(script: str, *, env_extra: dict | None = None, timeout: float = 60.0):
    home = Path(tempfile.mkdtemp(prefix="jev-unit-"))
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "INSTALL_SH": str(INSTALL),
        "TERM": "dumb",
    }
    if env_extra:
        env.update(env_extra)
    try:
        completed = subprocess.run(
            ["bash", "-c", UNIT_PREAMBLE + script],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
        return completed.returncode, completed.stdout, completed.stderr
    finally:
        shutil.rmtree(home, ignore_errors=True)


def run_install(
    args: list[str],
    *,
    seed_key: str | None = None,
    with_env_key: bool = True,
    timeout: float = 120.0,
):
    """Run the real installer with --dry-run against this checkout."""
    home = Path(tempfile.mkdtemp(prefix="jev-install-"))
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "TERM": "dumb",
    }
    if with_env_key:
        env["TYPESAFE_API_KEY"] = "ts_installer_test"
    if seed_key is not None:
        key_dir = home / ".config" / "typesafe"
        key_dir.mkdir(parents=True, exist_ok=True)
        (key_dir / "jev.env").write_text(f"TYPESAFE_API_KEY={seed_key}\n", encoding="utf-8")
    try:
        completed = subprocess.run(
            [str(INSTALL), "--global", "--repo", str(ROOT), "--dry-run"] + args,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
        return completed.returncode, completed.stdout + completed.stderr
    finally:
        shutil.rmtree(home, ignore_errors=True)


def selected_gates(output: str) -> list[str] | None:
    match = re.search(r"record gates \[([^\]]*)\]", output)
    if not match:
        return None
    return [gate for gate in match.group(1).split(",") if gate]


def pick(events_answer: str | None) -> tuple[int, str, str]:
    """Call the picker with a stubbed prompt_read."""
    if events_answer is None:
        stub = "prompt_read() { return 1; }"
    else:
        stub = f"prompt_read() {{ printf -v \"$1\" '%s' {events_answer!r}; return 0; }}"
    script = f"""
{stub}
events=""
select_events_interactively
printf 'EVENTS=%s\\n' "$events"
"""
    return run_bash(script)


class GateParsingTests(unittest.TestCase):
    def test_single_gate(self) -> None:
        code, out, err = run_bash(
            'events=""; add_events_from_list "BeforeTool"; printf "EVENTS=%s\\n" "$events"'
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "EVENTS=BeforeTool")

    def test_spaces_commas_and_case(self) -> None:
        code, out, err = run_bash(
            'events=""; add_events_from_list "3, afteragent"; printf "EVENTS=%s\\n" "$events"'
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "EVENTS=BeforeAgent,AfterAgent")

    def test_numbers_map_to_gate_order(self) -> None:
        code, out, err = run_bash(
            'events=""; add_events_from_list "1 4"; printf "EVENTS=%s\\n" "$events"'
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "EVENTS=AfterAgent,SessionStart")

    def test_duplicates_are_dropped(self) -> None:
        code, out, err = run_bash(
            'events=""; add_events_from_list "AfterAgent,AfterAgent,sessionstart";'
            ' printf "EVENTS=%s\\n" "$events"'
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "EVENTS=AfterAgent,SessionStart")

    def test_all_expands_to_every_gate(self) -> None:
        code, out, err = run_bash(
            'events=""; add_events_from_list "all"; printf "EVENTS=%s\\n" "$events"'
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "EVENTS=" + ",".join(ALL_GATES))

    def test_unknown_gate_is_rejected(self) -> None:
        code, _, err = run_bash('events=""; add_events_from_list "Nope"')
        self.assertNotEqual(code, 0)
        self.assertIn("unknown gate", err)

    def test_out_of_range_number_is_rejected(self) -> None:
        code, _, err = run_bash('events=""; add_events_from_list "9"')
        self.assertNotEqual(code, 0)
        self.assertIn("invalid gate number", err)


class InteractivePickerTests(unittest.TestCase):
    def test_default_is_after_agent(self) -> None:
        code, out, err = pick("")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "EVENTS=AfterAgent")
        self.assertIn("AfterAgent", err, "the menu should list the gates")

    def test_multi_select_by_number(self) -> None:
        code, out, err = pick("2 3")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "EVENTS=BeforeTool,BeforeAgent")

    def test_all_by_name(self) -> None:
        code, out, err = pick("all")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "EVENTS=" + ",".join(ALL_GATES))

    def test_missing_terminal_falls_back_to_after_agent(self) -> None:
        code, out, err = pick(None)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "EVENTS=AfterAgent")
        self.assertIn("no terminal", err)

    def test_menu_describes_every_gate(self) -> None:
        _, _, err = pick("")
        for name in ALL_GATES:
            self.assertIn(name, err)


class DryRunFlowTests(unittest.TestCase):
    def test_explicit_gates_are_recorded(self) -> None:
        code, output = run_install(["--events", "AfterAgent,BeforeTool"])
        self.assertEqual(code, 0, output)
        self.assertEqual(selected_gates(output), ["AfterAgent", "BeforeTool"])
        self.assertIn("keep gates [AfterAgent,BeforeTool]", output)

    def test_all_gates(self) -> None:
        code, output = run_install(["--events", "all"])
        self.assertEqual(code, 0, output)
        self.assertEqual(selected_gates(output), ALL_GATES)

    def test_repeated_flag_accumulates(self) -> None:
        code, output = run_install(["--events", "AfterAgent", "--events", "SessionStart"])
        self.assertEqual(code, 0, output)
        self.assertEqual(selected_gates(output), ["AfterAgent", "SessionStart"])

    def test_unknown_gate_fails_before_doing_anything(self) -> None:
        code, output = run_install(["--events", "Nope"])
        self.assertNotEqual(code, 0)
        self.assertIn("unknown gate", output)


class ApiKeyReuseTests(unittest.TestCase):
    """Re-running to change gates must not ask for the key again."""

    def test_stored_key_is_reused(self) -> None:
        code, output = run_install(
            ["--events", "all"], seed_key="apikey_stored", with_env_key=False
        )
        self.assertEqual(code, 0, output)
        self.assertIn("reusing the stored key", output)
        self.assertEqual(selected_gates(output), ALL_GATES)

    def test_explicit_key_wins_over_the_stored_one(self) -> None:
        code, output = run_install(
            ["--events", "all", "--api-key", "apikey_explicit"],
            seed_key="apikey_stored",
            with_env_key=False,
        )
        self.assertEqual(code, 0, output)
        self.assertNotIn("reusing the stored key", output)

    def test_environment_key_is_used_without_touching_the_file(self) -> None:
        code, output = run_install(["--events", "all"], with_env_key=True)
        self.assertEqual(code, 0, output)
        self.assertNotIn("reusing the stored key", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
