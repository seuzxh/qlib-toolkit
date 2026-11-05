# H20 Linux profile

The built-in profile derives account-specific paths at runtime instead of
embedding a username in the plugin:

- Python defaults to `~/miniconda3/envs/qlib_ifind_beta/bin/python`; override
  it with `QLIB_IFIND_PYTHON`.
- The day provider defaults to `~/.qlib/qlib_data/cn_data`; override it with
  `QLIB_IFIND_DAY_DATA`.
- The minute provider defaults to `~/.qlib/qlib_data/cn_data_1min`; override
  it with `QLIB_IFIND_MINUTE_DATA`.
- Allowed project roots default to `~/projects` and
  `${CODEX_HOME:-~/.codex}/worktrees`.

These are profile defaults, not verified facts about a new machine. Before
apply, inspect the generated plan and use `--profile-file` when the target
host uses another layout, environment manager, or Qlib version.

Credential references are names only: `QLIB_IFIND_TOKEN_FILE` and `IFIND_REFRESH_TOKEN`. Never read or print their values during initialization or doctor.

The investigated project uses backward-adjusted prices. That is a project/profile choice, not a universal Qlib default. Absolute volume units remain unverified and must stay `unknown`. The raw `.bin` contract is little-endian float32 with the first float as calendar start index.

Do not call `build_overlay.py`, Recorder, training, reports, hooks or account replay. The default doctor reads small raw calendar, instrument and feature samples without `qlib.init`.
