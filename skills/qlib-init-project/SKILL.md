---
name: qlib-init-project
description: Use when initializing or safely completing a Qlib factor-research workspace, especially when reusing existing providers, preserving an existing project, or applying the user's Qlib directory and AGENTS.md conventions.
---

# Initialize a Qlib Project

Create only a reusable research shell. Do not copy an old strategy, choose factors or labels, install packages, build an overlay, train, backtest, enable hooks, or write a shared provider.

## Workflow

1. Read [references/config-schema.md](references/config-schema.md) when choosing bootstrap inputs or explaining the generated manifest.
2. Read [references/h20-linux-profile.md](references/h20-linux-profile.md) when targeting H20. Treat paths as machine facts and credential names as references, never values.
3. Run `python scripts/qlib_init_project.py plan ...`. Review stderr and the JSON actions. A plan may write only an explicitly named plan file outside the target.
4. Show the plan hash, creates, conflicts, blocked files, and unknown contracts. Obtain confirmation of that exact hash before apply.
5. Run `python scripts/qlib_init_project.py apply --plan-file <file> --confirm-plan-hash <hash>` only on a Linux runtime that passes the script's safety capability checks.
6. Run `doctor` and report structure, environment, and data separately. A blocked data check does not invalidate a successfully created shell.

Use `dry-run` only as an alias of `plan`. Never hand-edit a plan JSON or substitute a newly computed hash for the one the user reviewed.

## Existing projects

Preserve every user-authored byte outside the versioned managed blocks. A different non-managed file is a conflict, not an overwrite request. Managed-block drift blocks only that file. Read [references/agents-contract.md](references/agents-contract.md) for merge and drift rules.

## Evidence boundary

Read [references/validation-contract.md](references/validation-contract.md) before claiming success. `apply` proves only the filesystem contract; `doctor environment` proves importability; `doctor data` proves only the sampled raw files. None proves causal factors, valid training, trustworthy backtests, or strategy profitability.

## Commands

```text
python scripts/qlib_init_project.py plan --target <project> --project-name <name> --package <package> --machine-profile h20-linux --plan-file <outside-target.json>
python scripts/qlib_init_project.py apply --plan-file <outside-target.json> --confirm-plan-hash <hash>
python scripts/qlib_init_project.py doctor --target <project> --layer all
```

Run `python scripts/qlib_init_project.py --help` for the current interface. Report unknown adjustment or volume contracts as `unknown`; do not infer them from file existence.
