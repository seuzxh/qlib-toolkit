# Configuration schema

## Layers

- `qlib-project.toml` is portable and owns project name, package, environment constraints, data contracts, local output paths and requested-only permissions.
- `config/machines/local.toml` owns interpreter and provider absolute paths plus allowed project roots. It is ignored by Git.
- `config/strategies/*.toml` owns strategy choices only. The initializer leaves the active strategy empty.

Facts must not silently override across layers. Unknown fields, wrong types, unsupported schema versions and malformed TOML fail closed.

## Bootstrap

For a new project provide target, project name, Python package and either the built-in `h20-linux` profile or an explicit profile file. Subsequent plans may read the generated manifest and local profile.

Environment modes are declarations: `reuse` records an existing interpreter; `declare` may add environment files; `create` may describe a later action. Version 1 never installs or creates an environment.

`permissions` records requested intent only. It never grants network, installation, provider write, training or trading authority.

## Data contract

Providers must be external read-only references. Keep frequency, URI environment-variable name, adjustment, field units, calendar source, instrument source and missing-data policy explicit. Use `unknown` when independent evidence is absent.
