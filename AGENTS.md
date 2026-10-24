# AGENTS.md

## Tasks

- mise is the task runner: run workflows with `mise run <task>` and list them with `mise tasks`.
- Put a short command in `[tasks]` in `mise.toml` and a script with logic in `mise-tasks/<name>`.
- Reuse an existing task before adding one; package scripts stay and tasks call them. Add no Make, just or Task files.
