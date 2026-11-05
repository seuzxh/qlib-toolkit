# Managed-file contract

`AGENTS.md` and `.gitignore` use separate `qlib-init-project` versioned managed blocks with a SHA-256 of the normalized managed body.

- Missing file: create the managed block.
- Existing file without markers: append the block and preserve the existing bytes.
- Current valid block: skip.
- Valid older block: replace only that block.
- Body hash mismatch, duplicate markers or damaged markers: block that file and preserve it.
- A semantic contradiction outside the block is reported for user resolution; it is never rewritten automatically.

All other existing files are non-managed: identical content is skipped, different content is a conflict. Apply never moves, renames or deletes user files, and never performs Git operations.
