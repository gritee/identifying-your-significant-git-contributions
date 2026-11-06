---
name: identifying-your-significant-git-contributions
description: Analyses one author's commits in a local git repository, interactively, to find their most significant contributions. Exports every commit to a CSV of significance metrics, runs 11 ranking analyses plus a composite score into a summary.md, reports the top 10 contributions, and optionally writes them up in the STAR format (Situation, Task, Action, Result). Use when asked to find, rank or summarise someone's most significant/impactful/important commits or contributions, prepare accomplishments for a review, résumé or interview from git history, or "run the contribution analysis".
compatibility: Requires git 2.31+ and python3 3.8+ with pandas.
---

# Git contribution significance

Finds an author's most significant contributions to a git repository and, optionally, writes them up as STAR stories.

## Requirements

The host must have all three; check them in step 1 and stop if any is missing.

| Dependency | Version | Used by |
|---|---|---|
| `git` | 2.31 or newer (needs `--diff-merges`) | `git_contributions_csv.py` |
| `python3` on `PATH` | 3.8 or newer | both scripts |
| `pandas` | 1.x or newer, importable from `python3` | `rank_contributions.py` |

## Bundled scripts

Both live in `${CLAUDE_SKILL_DIR}/scripts/`. Below, `$PY` means `python3` (or the interpreter the user names in step 1).

- `git_contributions_csv.py`: writes one CSV row per commit (about 125 columns): message text verbatim, size, spread, file categories, history context, reverts and percentiles. Standard library only. Run `--help` for options.
- `rank_contributions.py`: filters noise, recomputes percentiles and writes the top 20 commits for each of 11 rankings, plus the composite score, appearances list and `summary.md`. Needs pandas. Run `--help` for options.

All output goes in an output directory, `<out>`, chosen in step 5. It is never inside the analysed repository.

## Workflow

Work through these steps in order. Steps 2–4 are interactive: ask with the AskUserQuestion tool when it is available, otherwise ask in plain text and wait for the answer.

### 1. Check dependencies

```bash
git --version
python3 -c 'import sys, pandas; print(sys.version.split()[0], pandas.__version__)'
```

- If git is missing or older than 2.31, stop and tell the user.
- If `python3` is missing, older than 3.8, or can't import pandas, stop and tell the user. They can install pandas (`python3 -m pip install pandas`) or give the path of a Python 3 interpreter that has it; if they give one, re-run the check with it and use it as `$PY`. Don't install anything yourself without asking.

### 2. Get the repository

Ask for the **absolute local path** of the git repository. Only local paths are supported: if the user gives a URL (`https://`, `git@`, `ssh://`), say so and ask them to clone it and give the path. Validate it:

```bash
git -C "<path>" rev-parse --show-toplevel
```

If the path is relative, not a repository, or fails, say what went wrong and ask again. Use the `--show-toplevel` result as `<repo>`.

### 3. Identify the author

Ask who to analyse (a name, an email, or both). Then compare it with the repository's actual contributors:

```bash
git -C "<repo>" shortlog -sne --all --use-mailmap
```

Find every identity that plausibly belongs to the person: the same name with different emails, the same email with different names, and close variants (initials, usernames, personal and work addresses). Then:

- **One clear match:** tell the user which identity and commit count you'll use, and carry on.
- **Several identities for the same person** (e.g. a work and a personal email): list them with counts and ask which to include. Suggest including all of them.
- **Near-misses that may be other people** (e.g. "Jane Doe" vs "Jane Dorsey", or a shared bot or team account): show them and ask before including any.
- **No match:** show the closest names and ask the user to clarify.

Build one `--author` argument per chosen identity. Prefer exact emails with regex metacharacters escaped (`jane\.doe@example\.com`), because git treats `--author` as a regex matched against `Name <email>`. A bare name works only if no other contributor's name contains it.

### 4. Choose the history to analyse

Find the default branch:

```bash
git -C "<repo>" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null || git -C "<repo>" branch --list master main
```

Ask which history to use, suggesting the default branch first:
- **Default branch** (recommended): only commits that landed. Pass `--rev <branch>` (e.g. `--rev master` or `--rev origin/master`).
- **Current HEAD:** pass `--rev HEAD`.
- **Another branch:** `--rev <name>`.
- **All branches:** `--all`. Warn that this adds unmerged work and repeated copies of rebased commits.

Also offer an optional date range (`--since` / `--until`), for example for a review period. Pass it to the CSV script in step 5; the ranking script then needs no range, because the CSV only contains commits in it.

### 5. Generate the CSV

Choose `<out>`, the output directory. Use `git-contributions/` in the current working directory, unless the current directory is `<repo>` or inside it (compare `git rev-parse --show-toplevel` with `<repo>`). In that case use `~/git-contributions/` instead, so no output lands in the analysed repository, and tell the user. Use the absolute path of `<out>` from here on.

```bash
"$PY" "${CLAUDE_SKILL_DIR}/scripts/git_contributions_csv.py" -C "<repo>" \
  --author "<identity 1>" [--author "<identity 2>" ...] \
  --rev "<branch>" [--since ... --until ...] --with-tags \
  -o "<out>/<repo-name>.csv"
```

`<repo-name>` is the basename of `<repo>`. On a large repository (tens of thousands of commits) this can take several minutes: run it with a long timeout or in the background. If it prints `no commits matched`, go back to step 3.

### 6. Run the ranking analysis

```bash
"$PY" "${CLAUDE_SKILL_DIR}/scripts/rank_contributions.py" "<out>/<repo-name>.csv" \
  --out-dir "<out>" --top 20 --quiet
```

Always use `--top 20`, so that each of the 11 rankings, the composite table and the appearances table lists 20 commits and every summary is comparable. The top 10 in step 7 comes from these top-20 lists. Keep the default filters (merges, merge bookkeeping, WIP/fixup, reverts, mechanical sweeps, empty commits and rebase duplicates are excluded) unless the user asks otherwise.

`summary.md` lists the number of eligible commits for each ranking. If any ranking has fewer than 20, tell the user which one and how many it has.

Then tell the user where the output is, using absolute paths:

- `<out>/<repo-name>.csv`: the full per-commit export
- `<out>/summary.md`: all rankings in one document
- `<out>/ranking_*.csv`: one file per ranking (11)
- `<out>/composite.csv`, `appearances.csv`, `filtered.csv`, `excluded.csv`

Mention that a second run in the same directory overwrites these files.

### 7. Report the top 10

Read `<out>/summary.md` in full, then pick the 10 most significant contributions that emerge from it:

1. **Shortlist:** commits in the **appearances** table with at least 3 ranking appearances that also have either a composite score in the top 20 of the **composite** table, or 5+ appearances. Breadth across rankings counts as much as the composite score.
2. **Sanity check each candidate** against its numbers and title, and set aside commits that are big only for mechanical reasons:
   - test or fixture infrastructure whose size comes from many files near fixtures
   - commits whose spread score is near 0 even though they touch many files (one file or kind of file dominates)
   - anything that's really bookkeeping and got past the filters
3. **Order** by appearances, then composite score, then use judgement: a framework or platform upgrade, a new integration or a new product capability outranks a same-sized internal change.
4. **Look for what the metrics undervalue:**
   - a series of consecutive commits on one theme, such as a multi-step upgrade (Rails 6.0 → 6.1 → 7.0 …), that is small per commit but significant as a whole
   - companion commits for the same feature
   - families of similar work (several vendor or third-party integrations)
   - the largest pure cleanups from **cleanup_simplification**

Present:
- **A table:** `#`, short SHA, date (YYYY-MM), title, and a one-line "why it stands out" that cites the real numbers (rankings appeared in, composite score, effective lines changed, new files, modules, test lines).
- **"Worth adding, but undervalued by the metrics":** the items from step 4.
- **"Left out on purpose":** anything that ranked high but you set aside, with the reason.
- **One caveat:** the rankings measure the shape of each change, not its business impact, so the user should check the order.

### 8. Offer STAR write-ups

Ask whether to write up the top 10 in the STAR format. Offer:
- **Yes**
- **Yes, with extra context:** invite the user to add anything the git history can't show, such as business outcomes, metrics, team size, deadlines, which roles to target, or which commits to drop or add.
- **No:** finish here.

If they say yes, gather evidence for each commit before writing. Run these against `<repo>`:

```bash
git -C "<repo>" show -s --format='%an%n%ad%n%n%B' <sha>     # full message
git -C "<repo>" show --stat --format= -M <sha> | tail -25   # what changed
git -C "<repo>" log --oneline -5 --format='%h %ad %s' --date=short <sha>~1   # what came before
```

Use the matching row in `<out>/<repo-name>.csv` for figures, and read key parts of the diff where the message is thin. For a series or companion group, write one story for the whole group.

Write each one as follows:

- **Situation (S):** 1–2 sentences of context and background: where and when (the product or system area, and the month and year).
- **Task (T):** the person's specific responsibility, goal, or the problem that needed solving.
- **Action (A):** the concrete steps, their personal contributions, and the tools and technologies they used. This is the bulk of the story, about 60% of it. Always use "I" statements, never "we".
- **Result (R):** the positive outcome, ideally backed by numbers or key takeaways.

Rules for the write-ups:
- Write in the first person, as the author.
- Ground every claim in the commit message, the diff or what the user said. Don't invent business outcomes. Numbers from git are fine ("added 22 files and 869 lines of tests across 12 modules"). Mark any business result you can't verify as a placeholder, e.g. `[metric: e.g. % fewer manual exports]`, so the user can fill it in.
- Use the user's extra context wherever it applies, and let it take precedence over what you inferred.
- Keep each story to about 150–220 words. Put the short SHA and title above each one.

Show all the write-ups in the conversation, and also save them to `<out>/star.md`: a heading per contribution with S, T, A and R subheadings, then a short list of the placeholders the user still needs to fill. Tell the user the file's absolute path.
