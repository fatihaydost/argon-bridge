# Release checklist

Before every release (tag, public push, marketplace update):

- [ ] CI green on Linux and macOS (`.github/workflows/test.yml`).
- [ ] `python3 -m unittest -q` green locally (fake `agy`, no network).
- [ ] `claude plugin validate .` passes.
- [ ] **Worker smoke run, real `agy`:** in a scratch git repo with `slug.py` (`slugify` = lowercase + spaces to
      dashes) and `tests/test_slug.py`, delegate the Turkish `slugify` task in two phases (1: a failing test for
      `slugify("Çağrı Işık") == "cagri-isik"`, check expects `FAILED (failures=1)`; 2: implement, check expects
      `OK`) with `--skills ponytail,test-driven-development` and `permissions on`. Confirm:
  - the phase reports show the test written first (phase 1 touches only the test file);
  - the implementation is a small change;
  - no denied command and nothing touching the outside world in the reports or the diff;
  - `delegate check … pass` commits carry the user's identity and no AI mention;
  - `delegate merge` brings the work into the user's branch and removes the worktree and branch;
  - `~/.gemini/antigravity-cli/settings.json` unchanged, no `argon-bridge-*` project file left behind.
- [ ] **Reviewer smoke run:** one commit of a non-small change in a scratch repo; the background review's result
      reaches Claude (next prompt, Bash call or Stop).

Record each worker smoke run below: date, `agy` version, job id, phase durations, Argon tokens, verdict.

## Smoke runs

### 2026-10-23 · agy 1.4.6 · worker effort medium

Scratch repo, plan as in the checklist, `--skills ponytail,test-driven-development`, `permissions on`.

| Run | Job | Result |
| --- | --- | --- |
| 1 | `turkish-slug-202610231328` | Phase 1 done in 54 s (test only, check `FAILED (failures=1)`), committed. Phase 2 (resumed conversation) ended `denied` twice: first on `cd <worktree> && python3 -m unittest …` (the `cd` prefix misses the exact allow rule), then on the plain allowed command. Cause: a resumed `agy` conversation keeps its first turn's project id, whose per-run file was gone. Fixed (no `cd` prefix; one `agy` project per job). Rejected. 163 159 Argon tokens. |
| 2 | `turkish-slug-202610231334` | Phase 1 `failed` after 150 s: the skills folder passed with `--add-dir` held `.agents/`, so `agy` ran every command there instead of the worktree. Fixed (skills copied into the worktree's `.agents/skills/`, kept out of git). Rejected. 47 875 tokens. |
| 3 | `turkish-slug-202610231340` | **Pass.** Phase 1: 41 s, test first, only `tests/test_slug.py` changed, check `FAILED (failures=1)`. Phase 2 (same conversation): 38 s, one-line `str.maketrans` change in `slug.py`, check `OK`. No fix rounds, no denied command, nothing touching the outside world, subagents none. Commits `Add a failing test for Turkish letters in slugify`, `Map Turkish letters to ASCII in slugify`, merge `Merge turkish-slug`: author Fatih Aydost, no AI mention. Merge into `main` worked; worktree, branch, exclude lines and the job's `agy` project file removed; `settings.json` and `~/.gemini/config/projects/` unchanged (`cmp`, `diff -r`). 73 657 Argon tokens. |

Reviewer smoke run: every worker-role commit in this repo was reviewed in the background and the findings reached
Claude at the next Bash call or prompt.
