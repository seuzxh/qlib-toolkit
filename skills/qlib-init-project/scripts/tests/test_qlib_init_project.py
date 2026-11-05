from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import struct
import sys
import tempfile
import tomllib
import unittest


SCRIPT = Path(__file__).parents[1] / "qlib_init_project.py"
SPEC = importlib.util.spec_from_file_location("qlib_init_project", SCRIPT)
assert SPEC and SPEC.loader
init = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = init
SPEC.loader.exec_module(init)


class CanonicalPlanTests(unittest.TestCase):
    def test_hash_is_deterministic_and_ignores_display_fields(self):
        plan = {"schema_version": 1, "actions": [{"path": "a", "status": "create"}], "display_metadata": {"time": "one"}}
        first = init.with_plan_hash(plan)
        changed = dict(first, display_metadata={"time": "two"})
        self.assertEqual(first["plan_hash"], init.compute_plan_hash(changed))
        self.assertEqual(init.canonical_json(first), init.canonical_json(init.with_plan_hash(plan)))

    def test_strict_json_rejects_duplicate_keys_constants_and_large_input(self):
        for raw in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(init.ConfigError):
                init.strict_json_loads(raw)
        with self.assertRaises(init.ConfigError):
            init.strict_json_loads(" " * (init.MAX_PLAN_BYTES + 1))

    def test_toml_string_round_trips_hostile_text(self):
        value = 'name "quoted"\\path\nnext'
        parsed = tomllib.loads("value = " + init.toml_string(value))
        self.assertEqual(value, parsed["value"])


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.allowed = self.root / "projects"
        self.allowed.mkdir()
        self.target = self.allowed / "alpha"
        self.profile = init.machine_profile(
            "test",
            python=sys.executable,
            day_provider=str(self.root / "day"),
            minute_provider=str(self.root / "minute"),
            allowed_roots=[str(self.allowed)],
        )

    def request(self, **overrides):
        values = dict(target=str(self.target), project_name="alpha", package="alpha_research", profile=self.profile)
        values.update(overrides)
        return init.BootstrapRequest(**values)

    def test_builtin_profile_derives_paths_from_the_selected_home(self):
        profile = init.builtin_profile("h20-linux", home="/home/alice", environ={})

        self.assertEqual(
            "/home/alice/miniconda3/envs/qlib_ifind_beta/bin/python",
            profile["environment"]["python"],
        )
        self.assertEqual(
            {
                "QLIB_IFIND_DAY_DATA": "/home/alice/.qlib/qlib_data/cn_data",
                "QLIB_IFIND_MINUTE_DATA": "/home/alice/.qlib/qlib_data/cn_data_1min",
            },
            profile["data_paths"],
        )
        self.assertEqual(
            ["/home/alice/projects", "/home/alice/.codex/worktrees"],
            profile["runtime"]["allowed_project_roots"],
        )

    def test_builtin_profile_uses_explicit_runtime_path_overrides(self):
        profile = init.builtin_profile(
            "h20-linux",
            home="/home/alice",
            environ={
                "CODEX_HOME": "/srv/codex",
                "QLIB_IFIND_PYTHON": "/opt/qlib/bin/python",
                "QLIB_IFIND_DAY_DATA": "/data/qlib/day",
                "QLIB_IFIND_MINUTE_DATA": "/data/qlib/minute",
            },
        )

        self.assertEqual("/opt/qlib/bin/python", profile["environment"]["python"])
        self.assertEqual("/data/qlib/day", profile["data_paths"]["QLIB_IFIND_DAY_DATA"])
        self.assertEqual("/data/qlib/minute", profile["data_paths"]["QLIB_IFIND_MINUTE_DATA"])
        self.assertEqual(
            ["/home/alice/projects", "/srv/codex/worktrees"],
            profile["runtime"]["allowed_project_roots"],
        )

    def test_plan_does_not_create_target(self):
        plan = init.build_plan(self.request())
        self.assertFalse(self.target.exists())
        self.assertTrue(any(a["status"] == "create" for a in plan["actions"]))
        self.assertEqual(plan["plan_hash"], init.compute_plan_hash(plan))

    def test_plan_has_only_generic_research_shell(self):
        raw = init.canonical_json(init.build_plan(self.request())).decode()
        for forbidden in ("Alpha158", "LightGBM", "Top10", "883926", "T+2", "build_overlay", "hook"):
            self.assertNotIn(forbidden, raw)
        self.assertIn("unknown", raw)
        self.assertIn("artifacts/mlruns", raw)

    def test_target_escape_root_and_home_are_rejected(self):
        with self.assertRaises(init.PathSafetyError):
            init.build_plan(self.request(target=str(self.allowed / ".." / "escape")))
        with self.assertRaises(init.PathSafetyError):
            init.validate_target(Path.home(), [Path.home()])

    def test_provider_cannot_be_write_target(self):
        provider = self.root / "provider"
        provider.mkdir()
        profile = init.machine_profile("test", python=sys.executable, day_provider=str(provider), minute_provider=str(provider), allowed_roots=[str(self.root)])
        with self.assertRaises(init.PathSafetyError):
            init.build_plan(self.request(target=str(provider / "child"), profile=profile))
        profile_inside = init.machine_profile("test", python=sys.executable, day_provider=str(self.target / ".qlib-init"), minute_provider=str(self.root / "minute"), allowed_roots=[str(self.allowed)])
        with self.assertRaises(init.PathSafetyError):
            init.build_plan(self.request(profile=profile_inside))

    def test_unknown_profile_keys_fail_closed(self):
        profile = dict(self.profile)
        profile["permissions"] = {"data_write": True}
        with self.assertRaises(init.ConfigError):
            init.build_plan(self.request(profile=profile))
        malformed = dict(self.profile)
        malformed["environment"] = {"manager": "none", "name": ""}
        with self.assertRaises(init.ConfigError):
            init.build_plan(self.request(profile=malformed))

    def test_existing_manifest_schema_rejects_unknown_types_and_permissions(self):
        manifest = tomllib.loads(init.render_manifest(self.request()))
        init.validate_manifest_schema(manifest)
        cases = []
        unknown = json.loads(json.dumps(manifest)); unknown["unexpected"] = 1; cases.append(unknown)
        wrong = json.loads(json.dumps(manifest)); wrong["project"]["package"] = 42; cases.append(wrong)
        writable = json.loads(json.dumps(manifest)); writable["data"]["providers"]["day"]["read_only"] = False; cases.append(writable)
        permitted = json.loads(json.dumps(manifest)); permitted["permissions"]["network"] = True; cases.append(permitted)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(init.ConfigError):
                init.validate_manifest_schema(case)

    def test_secret_environment_values_never_enter_plan(self):
        old = {key: os.environ.get(key) for key in init.SECRET_ENV_NAMES}
        try:
            for key in init.SECRET_ENV_NAMES:
                os.environ[key] = "SENTINEL-SHOULD-NOT-APPEAR"
            raw = init.canonical_json(init.build_plan(self.request())).decode("utf-8")
            self.assertNotIn("SENTINEL-SHOULD-NOT-APPEAR", raw)
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_existing_nonmanaged_file_conflicts_without_overwrite(self):
        self.target.mkdir()
        (self.target / "pyproject.toml").write_text("user content\n", encoding="utf-8")
        plan = init.build_plan(self.request())
        action = next(a for a in plan["actions"] if a["path"] == "pyproject.toml")
        self.assertEqual("conflict", action["status"])
        self.assertEqual("user content\n", (self.target / "pyproject.toml").read_text(encoding="utf-8"))

    def test_managed_append_preserves_user_bytes_and_drift_blocks_only_file(self):
        self.target.mkdir()
        original = b"# user\r\nkeep=true\r\n"
        (self.target / "AGENTS.md").write_bytes(original)
        first = init.build_plan(self.request())
        action = next(a for a in first["actions"] if a["path"] == "AGENTS.md")
        self.assertEqual("create", action["status"])
        self.assertTrue(action["content"].encode("utf-8").startswith(original))
        (self.target / "AGENTS.md").write_text(init.render_managed_file("AGENTS.md", ""), encoding="utf-8")
        text = (self.target / "AGENTS.md").read_text(encoding="utf-8").replace("先核对", "用户改过")
        (self.target / "AGENTS.md").write_text(text, encoding="utf-8")
        second = init.build_plan(self.request())
        drift = next(a for a in second["actions"] if a["path"] == "AGENTS.md")
        self.assertEqual("blocked", drift["status"])
        self.assertTrue(any(a["path"] != "AGENTS.md" for a in second["actions"]))

    def test_plan_file_is_exclusive(self):
        path = self.root / "plan.json"
        plan = init.build_plan(self.request())
        init.write_plan_file(path, plan)
        with self.assertRaises(FileExistsError):
            init.write_plan_file(path, plan)

    def test_apply_rejects_bad_confirmation_and_non_linux_runtime(self):
        plan = init.build_plan(self.request())
        with self.assertRaises(init.PlanError):
            init.apply_plan(plan, "0" * 64)
        if os.name != "posix":
            with self.assertRaises(init.PathSafetyError):
                init.apply_plan(plan, plan["plan_hash"])

    def test_plan_schema_rejects_absolute_and_parent_action_paths(self):
        for unsafe in ("/tmp/escape", "../escape", r"C:\\escape"):
            with self.subTest(unsafe=unsafe):
                plan = init.build_plan(self.request())
                plan["actions"][0]["path"] = unsafe
                plan = init.with_plan_hash(plan)
                with self.assertRaises(init.PlanError):
                    init._validate_plan(plan, plan["plan_hash"])

    def test_custom_profile_keeps_adjustment_unknown_and_manifest_sources_explicit(self):
        text = init.render_manifest(self.request())
        parsed = tomllib.loads(text)
        self.assertEqual("unknown", parsed["data"]["adjustment"])
        self.assertEqual("day", parsed["data"]["calendar"]["source"])
        self.assertEqual("project-profile", parsed["data"]["instruments"]["source"])

    def test_managed_symlink_is_blocked_without_disclosing_target(self):
        self.target.mkdir()
        external = self.root / "external-secret"
        external.write_text("DO-NOT-DISCLOSE", encoding="utf-8")
        try:
            os.symlink(external, self.target / "AGENTS.md")
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        plan = init.build_plan(self.request())
        action = next(a for a in plan["actions"] if a["path"] == "AGENTS.md")
        self.assertEqual("blocked", action["status"])
        self.assertNotIn("DO-NOT-DISCLOSE", init.canonical_json(plan).decode("utf-8"))

    def test_isolated_environment_does_not_forward_unrelated_secrets(self):
        old = os.environ.get("UNRELATED_SECRET")
        os.environ["UNRELATED_SECRET"] = "secret"
        try:
            env = init.isolated_environment(str(self.root / "temp"))
        finally:
            if old is None:
                os.environ.pop("UNRELATED_SECRET", None)
            else:
                os.environ["UNRELATED_SECRET"] = old
        self.assertNotIn("UNRELATED_SECRET", env)

    def test_apply_freshness_rejects_file_created_or_changed_after_plan(self):
        plan = init.build_plan(self.request())
        self.target.mkdir()
        (self.target / "pyproject.toml").write_text("concurrent file\n", encoding="utf-8")
        with self.assertRaises(init.PlanError):
            init.validate_plan_freshness(plan)

        (self.target / "pyproject.toml").unlink()
        (self.target / "AGENTS.md").write_text("user rules\n", encoding="utf-8")
        second = init.build_plan(self.request())
        (self.target / "AGENTS.md").write_text("changed later\n", encoding="utf-8")
        with self.assertRaises(init.PlanError):
            init.validate_plan_freshness(second)

    def test_apply_freshness_rejects_replaced_allowed_root(self):
        plan = init.build_plan(self.request())
        old = self.root / "old-projects"
        self.allowed.rename(old)
        self.allowed.mkdir()
        with self.assertRaises(init.PlanError):
            init.validate_plan_freshness(plan)


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_data_doctor_reads_fixture_without_writes(self):
        provider = self.root / "provider"
        (provider / "calendars").mkdir(parents=True)
        (provider / "instruments").mkdir()
        (provider / "features" / "sh600519").mkdir(parents=True)
        (provider / "calendars" / "day.txt").write_text("2026-01-05\n2026-01-06\n", encoding="utf-8")
        (provider / "instruments" / "all.txt").write_text("SH600519\t2026-01-05\t2026-01-06\n", encoding="utf-8")
        (provider / "features" / "sh600519" / "close.day.bin").write_bytes(struct.pack("<fff", 0.0, 10.0, 11.0))
        before = init.snapshot_tree(provider)
        result = init.doctor_data(provider, frequency="day", instrument="SH600519", field="close")
        self.assertEqual("pass", result["status"])
        self.assertEqual(0.0, result["start_index"])
        self.assertEqual(before, init.snapshot_tree(provider))

    def test_data_doctor_marks_missing_provider_blocked(self):
        result = init.doctor_data(self.root / "missing", frequency="day", instrument="SH600519", field="close")
        self.assertEqual("blocked", result["status"])

    def test_environment_doctor_uses_isolated_subprocess(self):
        result = init.doctor_environment(Path(sys.executable), expected_qlib=None)
        self.assertIn(result["status"], {"pass", "blocked"})
        self.assertEqual(str(Path(sys.executable)), result["python"])
        self.assertIn("python_version", result)


class CliTests(unittest.TestCase):
    def test_dry_run_and_plan_share_handler(self):
        parser = init.build_parser()
        p = parser.parse_args(["plan", "--target", "x", "--project-name", "x", "--package", "x", "--machine-profile", "h20-linux"])
        d = parser.parse_args(["dry-run", "--target", "x", "--project-name", "x", "--package", "x", "--machine-profile", "h20-linux"])
        self.assertIs(p.handler, d.handler)

    def test_argparse_rejects_abbreviated_options(self):
        parser = init.build_parser()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["plan", "--tar", "x"])

    def test_plan_file_inside_target_is_rejected_without_creating_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            allowed = root / "projects"
            allowed.mkdir()
            target = allowed / "alpha"
            profile_path = root / "profile.toml"
            profile_path.write_text(
                'schema_version = 1\nprofile = "test"\n'
                f'[environment]\npython = {init.toml_string(sys.executable)}\nmanager = "none"\nname = ""\n'
                f'[data_paths]\nQLIB_IFIND_DAY_DATA = {init.toml_string(str(root / "day"))}\nQLIB_IFIND_MINUTE_DATA = {init.toml_string(str(root / "minute"))}\n'
                '[credential_references]\ntoken_file_env = "QLIB_IFIND_TOKEN_FILE"\nrefresh_token_env = "IFIND_REFRESH_TOKEN"\n'
                f'[runtime]\nallowed_project_roots = [{init.toml_string(str(allowed))}]\n',
                encoding="utf-8",
            )
            args = init.build_parser().parse_args([
                "plan", "--target", str(target), "--project-name", "alpha", "--package", "alpha_research",
                "--profile-file", str(profile_path), "--plan-file", str(target / "plan.json"),
            ])
            with self.assertRaises(init.PathSafetyError):
                init.command_plan(args)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
