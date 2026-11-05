# Qlib Toolkit plugin

`qlib-toolkit` packages two independent skills:

- `qlib-reference` finds Qlib APIs, examples, architecture guidance, and
  version-sensitive best practices.
- `qlib-init-project` plans, applies, and diagnoses a minimal Qlib research
  workspace without installing packages, training models, running backtests,
  or writing shared providers.

The plugin contains no MCP server and needs no credentials. The initializer
stores only approved credential *environment-variable names*; it never reads
or packages their values.

## Install from GitHub

After this directory is pushed to the selected repository:

```bash
codex plugin marketplace add <owner>/<repo>
codex plugin marketplace list
```

Restart the ChatGPT desktop app, open the Plugins Directory, choose **Qlib
Toolkit**, and install `qlib-toolkit`.

For a local checkout, run:

```bash
codex plugin marketplace add /absolute/path/to/qlib-toolkit
```

The repository marketplace points at the plugin root (`./`).

## Use

```text
$qlib-reference 查找滚动训练的官方示例，并按本机版本核验。
$qlib-init-project 为新的因子研究目录生成初始化计划，不要修改共享数据。
```

`qlib-init-project` requires review of an exact plan hash before Linux apply.
Its built-in H20/Linux defaults derive from the current home directory. Set
`QLIB_IFIND_PYTHON`, `QLIB_IFIND_DAY_DATA`, `QLIB_IFIND_MINUTE_DATA`, or
`CODEX_HOME` when the host uses another layout; use `--profile-file` for a
fully explicit machine profile.

## Validate

```bash
python -m unittest discover -s skills/qlib-init-project/scripts/tests -p "test_*.py" -v
python skills/qlib-init-project/scripts/qlib_init_project.py --help
```

Validate both skill folders with the current Codex `skill-creator` validator
before publishing.

## Provenance and license

The reference skill preserves all nine resources from
`seuzxh/qlib-skill` commit `d458a334efdd8ac970ad70e8c81beb452b7e2473`,
renamed from `qlib` to `qlib-reference`. The initializer is the separately
developed `qlib-init-project` skill. The combined package is distributed
under the MIT License in [LICENSE](LICENSE).

This community plugin is not affiliated with Microsoft Qlib. Project-local
data contracts and installed-version evidence override its general guidance.
