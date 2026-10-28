# agy probes

Hand-run checks of `agy` behaviour the plugin relies on. Real `agy`, Gemini 4 Argon, effort low, `--mode plan
--sandbox`, in a scratch git repo. `~/.gemini/antigravity-cli/settings.json` was backed up first and restored
after every probe (`cmp` clean each time). New probes are appended here.

## Per-run permission rules (2026-10-23, agy 1.4.6)

Pass criterion: a deny rule keeps `SECRET=probe123` in `.env` out of Argon's answer.

| # | Mechanism | Result |
| --- | --- | --- |
| 0 | No rule (baseline) | Argon read `.env` and printed `probe123`. The reviewer could read secrets. |
| 1 | Global `settings.json`, `"permissions": {"deny": ["read_file(.env)"]}` | **Pass.** `-p "/permissions"` lists it under `global`; the read is refused with "Matches user-configured deny rule" and the turn goes on (a denied *read* does not end the turn, unlike a command that is not allowed). |
| 2 | `ANTIGRAVITY_CONFIG_REL_DIR` (exists in the binary) pointed at a dir with only `settings.json` (tried `name`, `.gemini/name`, absolute) | **Fail.** The rule never shows in any scope. Not usable. |
| 3 | Project scope | **Pass, per run.** Interactive `/permissions` → Project writes `~/.gemini/config/projects/<project>.json` as `"permissionGrants": {"permissionGrants": {"deny": [...]}}`. A plugin-written file `~/.gemini/config/projects/<id>.json` with that key, used with `agy --project <id>`, applies the rules (also with `"projectResources": {}`); the global file stays untouched and no other file under `~/.gemini` gains state (only agy's own logs and summaries). `-p "/permissions"` does not show `--project` rules, but runs obey them. |

**Verdict:** per-run mechanism found: a temporary project file plus `--project`. `permissions.hold` uses it
first; the global block stays as the fallback (when the project file cannot be written).

Side effects to know:
- `--project` replaces the folder's own project for that run, so the plugin copies that project's rules and
  settings (the deepest project holding the folder, else `default-cli-project.json`) into the temporary file.
- Probe 3 by hand added the rule to `default-cli-project.json` (the project of every folder without one); it was
  put back to its previous content in agy's own format (indent 2, no final newline); its bytes before the probe
  were not saved, so byte equality is not proven.
- Trusting the scratch folder in the interactive run added it to `trustedWorkspaces`; removed by the restore.

### Rule forms for `read_file`

`read_file(<path>)` is a **path prefix**: a file or a directory and everything under it. A relative path is
taken from the run's working directory. No patterns:

| Rule | Blocks |
| --- | --- |
| `read_file(.env)` | `./.env` only, not `sub/.env` |
| `read_file(.env.local)` | `./.env.local` |
| `read_file(/home/u/.argon-probe)` | every file below it (also with `--add-dir` on that dir) |
| `read_file(~/.argon-probe)` | nothing (`~` is not expanded) |
| `read_file(**/.env)`, `read_file(*.pem)`, `read_file(**/*.pem)` | nothing |
| `read_file(regex:.*\.(pem\|key\|p12)$)`, `read_file(regex:.*\.pem)`, `read_file(regex:.*[.]key$)` | nothing (`regex:` works for `command(...)` only) |

So `SECRET_DENY` holds absolute home paths (`~/.ssh`, `~/.aws`, `~/.config/gcloud`, …, expanded), and
`secret_rules(root)` adds one absolute rule per secret-looking file found under the repository (`.env`,
`.env.*` except examples, `*.pem`, `*.key`, `*.p12`, `*.pfx`). **The worker uses `secret_rules(worktree)`, not
`SECRET_DENY` alone.**

Other reads: in plan mode Argon has no grep or directory-listing tool of its own (`read_file`, `write_file`,
`edit_file`, `run_command`); `run_command` needs an allow rule, so shell reads of secrets are refused in headless
runs unless the user's own rules allow a reading command (`command(cat)` would bypass `read_file` rules).

## The worker (2026-10-23, agy 1.4.6)

Scratch git repo (`slug.py`, `tests/test_slug.py`, ignored `.env` with `SECRET=probe456`), effort `low`,
`--mode accept-edits --sandbox` unless noted, the permission guard on (`permissions.hold` with the worker's
rules → per-run `--project` file). `settings.json` and `~/.gemini/config/projects/` were backed up
before and compared after (`cmp` / `diff -r` clean); the user's folder stayed clean in every probe.

| # | Probe | Result |
| --- | --- | --- |
| 1 | Skills via `--add-dir <tmp>`, `<tmp>/.agents/skills/{ponytail,test-driven-development}` copies; `-p "List the skills available to you" --mode plan` | **Pass.** Lists `ponytail` and `test-driven-development` next to agy's own (`agy-customizations`, `antigravity-guide`, `impeccable`); without `--add-dir` only agy's own three. No file in the repo, global config untouched. Options 2 and 3 not needed. |
| 2a | Ask Argon to run `git push origin master` (denied by `command(git push)`) | The command is refused ("Permission denied for command(git push origin master). Matches user-configured deny rule.") and **the turn goes on**: Argon answers normally. No `denied_actions`. |
| 2b | Ask Argon to run `python3 -c 'print(41+1)'` (neither allowed nor denied) | **The turn ends**: `status: SUCCESS`, `response: ""`, and `--output-format json` adds `"denied_actions": [{"action": "command", "display_name": "RunCommand"}]` (no command text). stderr: `a tool required the "command" permission that headless mode cannot prompt for, so it was auto-denied`. With `--output-format stream-json` the last line is `{"event": "result", "result": {…same object…}}` and the command text is in the preceding `step_update` events: `step_update.tool_name == "run_command"`, `step_update.tool_info.parameters.CommandLine`. |
| 3 | Argon defines and invokes a subagent (`define_subagent`, `invoke_subagent`) that reads `slug.py` and `.env` with its file tool | `slug.py` read; `.env` refused inside the subagent ("Matches user-configured deny rule"). A subagent asked to run `git status`, `git push origin master`, `python3 -c …` ends the parent turn with `denied_actions` like 2b; the subagent's command lines do not appear in the parent's stream (denied command text unknown). A first attempt that said "secret" was blocked by Gemini's safety filter (inconclusive), the neutral rewording passed. |
| 4 | `--conversation <id>` with `--mode accept-edits`, `--output-format stream-json --json-schema` | First turn edited `slug.py`; `result.structured_output` held the report. Follow-up in the same conversation recalled the exact line it added (context kept). **Trap:** on the follow-up the answer was prose and `result.structured_output` was the *first* turn's object again (stale). |

Decisions:

- ~~`SKILL_DELIVERY = add_dir`~~ → **`SKILL_DELIVERY = worktree_exclude`** (changed after the smoke run, below).
- `SUBAGENTS_SAFE = yes` (deny rules and the headless "not allowed" stop both apply inside Argon's subagents; a
  denied command inside a subagent is reported without its text).
- The worker uses `--output-format stream-json`: the result is the last `event: result` line, the denied command is
  the `CommandLine` of the last `run_command` step when `denied_actions` is non-empty.
- The worker's report is `response` parsed as a JSON object; `structured_output` is a fallback only on a first turn
  (no `--conversation`), so a resumed turn's stale report is never read as a new one.

### Smoke run: a resumed conversation keeps its first project (2026-10-23)

A phase resumed with `--conversation <id>` and a **new** `--project` id (the per-run file, deleted after the
first turn) refused even an allowed command: `python3 -m unittest discover -s tests` matched
`command(python3 -m unittest discover -s tests)` in the new project, yet the turn ended with `denied_actions`. The
same command in a fresh conversation with the same file ran. With one fixed project id per job
(`argon-bridge-job-<id>`, rewritten before every run and removed after it), turn 1 and the resumed turn 2 both
ran the command. So the conversation stays bound to the project id of its first turn: `gateway.work` passes
`permissions.hold(..., name="job-<id>")`.

### Smoke run: a folder holding `.agents/` becomes the command folder (2026-10-23)

With the skills in `<job-skills>/.agents/skills/` passed as `--add-dir`, Argon's commands ran in `<job-skills>`, not
in the worktree (`ls` printed nothing; `python3 -m unittest discover -s tests`: "Start directory is not
importable"). Without that `--add-dir`, `ls` ran in the worktree; putting the worktree first as an extra
`--add-dir` did not help. Copies in the worktree's own `.agents/skills/<name>/`, kept out of git with
`/.agents/skills/<name>` lines in `info/exclude` (removed again with the worktree): `ls` ran in the worktree, both
skills listed, `git status` clean. So **`SKILL_DELIVERY = worktree_exclude`**; a skill the repository already has
there is never overwritten.

## What a denial looks like in stream-json (2026-10-24, agy 1.4.7)

Scratch git repo, effort `low`, `--mode accept-edits --sandbox --output-format stream-json`, a `--project` file
allowing only `ls`. Shortened outputs are the fixtures `tests/fixtures/agy_stream_*_denied.jsonl`.

| # | Probe | Result |
| --- | --- | --- |
| 1 | `read_file` of a file under `/tmp` outside the workspace | Read, no confirmation. |
| 2 | `read_file` of a file under `~` outside the workspace | **The turn ends at once** (6 s, no hang): `result.denied_actions: [{"action": "read_file", "display_name": "ReadFile"}]`, `response: ""`. The step is a `read_file` `step_update` with `tool_info.parameters.Path`, ACTIVE then DONE **without** `tool_info.output` (an allowed read has `output: "1 lines, 15 bytes"`). cli log: `tool_confirmation_manager.go:216] Print mode: soft-denying tool confirmation "ReadFile" at step 2`, then `server.go:2646] Tool confirmation for conversation <id> step 2 (type=*gemini_coder_go_proto.Step_Generic approved=false)`. |
| 3 | `ls && python3 -c 'print(41+1)'` (only `ls` allowed) | Turn ends, `denied_actions: [{"action": "command", "display_name": "RunCommand"}]`; the `run_command` step shows the whole `CommandLine`, not the part that was refused; DONE without `output`. Log line as in 2 with `"RunCommand"`. |
| 4 | Argon asked to start `sleep 30 && echo finished` in the background and read its log | It read `~/.gemini/antigravity-cli/brain/<conv>/.system_generated/tasks/task-2.log` with `read_file`: **allowed**, no rule needed. |

The 23.10 hang after a soft-denied ReadFile (agy 1.4.6) did not happen on 1.4.7: the denial ended the turn.
`gateway._stream` still stops a run after `stall_seconds` without any event, whatever the cause.
`brain/` is not added to the read rules: on 1.4.7 the read is already allowed, the conversation's folder is not
known before its first turn, and `brain/` holds every conversation's transcript (other repositories' code).

## A `&&` chain against exact-form rules (2026-10-24, agy 1.4.7)

Scratch git repo with `a.txt`, effort `low`, `--mode accept-edits --sandbox`, a `--project` file holding only the
allow rules below. Argon was told to run `ls && wc -l a.txt` once, as written.

| # | Allow rules | Result |
| --- | --- | --- |
| 1 | `command(regex:ls)`, `command(regex:wc -l a\.txt)` (the parts) | Ran; the answer held both outputs (`--output-format json`, so whether it ran as one command is not shown). |
| 2 | `command(regex:ls && wc -l a\.txt)` (the whole chain) | **Denied**: `denied_actions: [{"action": "command", …}]`, empty answer (json output: the command line Argon tried is not shown). |
| 3 | as 2, `--output-format stream-json` | Ran: the `run_command` step's `CommandLine` was `ls && wc -l a.txt`, the answer held both outputs. |

Mixed: the whole-chain rule passed once and failed once (2 may have tried another command line). So
`worker.check_rules` allows each safe part and the whole chain, both as exact forms; a part with quotes, `$`,
`;`, `|`, `&`, redirections or a backslash gets no rule (nor does its chain), and the lead runs that check.
