# Validation contract

| Check | Proves | Does not prove |
|---|---|---|
| `plan` | proposed actions, conflicts and deterministic hash | apply will succeed |
| `apply` | confirmed filesystem actions and managed-block contract | Qlib imports or data is valid |
| `doctor structure` | manifest, profile and managed blocks are readable | environment or data validity |
| `doctor environment` | selected interpreter can report Python and pyqlib/qlib | training correctness |
| `doctor data` | sampled raw calendar, instrument and `.bin` shape | full coverage, adjustment, units or unbiased data |

Doctor may write only an OS temporary directory used to isolate third-party caches. It must not persist files in the target project or providers. Version 1 does not call `qlib.init`, Recorder, training, backtest or report generation.

Fixture tests prove portable logic only. Claim H20 acceptance only after fresh tests on H20, an independent temporary project plan/apply/idempotence run, doctor evidence and before/after provider snapshots.
