# Identifying your significant git contributions

### me

Sometimes we need to contextualize our significant contributions to a codebase, but how do we identify them in the first place?

This is a claude skill to automate both finding and contextualizing your (or any author's) most "significant" contributions to a codebase.

This is a cleaned-up version of a rough first iteration I used: follows Claude skill conventions, is more tech-stack agnostic, removes more-private-content. I haven't run this cleaned-up version yet, but the rough one worked well, and thus worth sharing.

the functionality of the skill was inspired from [Matt Van Horn](https://github.com/mvanhorn)'s skills, and the emerging trend of leaning on CLIs for semi-programmatic workflow.

The skill essentially compiles a comparison CSV of all your (an author's) commits, performs various rankings, then select "significant" contributions as emergent from the rankings, and STAR-ifies them.

There are some limitations and tweaks to improve the skill noted below in the agent readme.

### agent

A [Claude Code](https://claude.com/claude-code) skill that finds your most significant contributions to a git repository and writes them up as STAR stories (Situation, Task, Action, Result).

Use it to get a head start on a performance review, résumé or interview prep. The results aren't final copy, but they save most of the digging.

## How it works

1. Exports each of your commits to a CSV of metrics: size, spread, file categories, history context, reverts and so on.
2. Filters out noise such as merges, WIP and fixup commits, reverts, lint and format sweeps, and rebase duplicates.
3. Ranks the remaining commits 11 ways, plus a composite score: authored volume, new functionality, architectural reach, core-code impact, greenfield work, work in unfamiliar code, cleanups, infrastructure upgrades, bug fixes, tested changes and well-documented commits.
4. Claude picks the top 10 contributions, favouring commits that rank high in many lists, and points out work the metrics undervalue, such as a series of small commits on one theme.
5. Optionally writes each one up in STAR format, using the commit messages and diffs plus any context you add.

The details are in [SKILL.md](SKILL.md) and the two scripts in [scripts/](scripts/).

## Requirements

- git 2.31 or newer
- Python 3.8 or newer, available as `python3`
- pandas (`python3 -m pip install pandas`)
- A local clone of the repository you want to analyse

## Install

Clone into your Claude Code skills directory:

```bash
git clone https://github.com/Gritee/identifying-your-significant-git-contributions.git ~/.claude/skills/identifying-your-significant-git-contributions
```

To make it available in one project only, clone it into that project's `.claude/skills/` folder instead.

## Usage

Run `/identifying-your-significant-git-contributions` in Claude Code, or ask something like:

> Find my most significant contributions to this repo

Then answer a few prompts:

1. The local path to the repository
2. Which author identities are yours (it lists the matching names and emails it finds)
3. Which branch to analyse, and an optional date range
4. Whether you want STAR write-ups, and any context git can't show, such as business outcomes or team size

## Output

Everything is written to `git-contributions/` in your current working directory. If you run it from inside the repository being analysed, it writes to `~/git-contributions/` instead, so nothing lands in that repository.

It writes these files:

| File | Contents |
|---|---|
| `<repo>.csv` | Every commit with about 125 metric columns |
| `summary.md` | All rankings in one document |
| `ranking_*.csv` | One file per ranking |
| `composite.csv`, `appearances.csv` | Composite score and cross-ranking appearances |
| `filtered.csv`, `excluded.csv` | Commits kept, and commits excluded with the reason |
| `star.md` | The STAR write-ups, if you asked for them |

Running it again from the same directory overwrites these files, even for a different repository; only `<repo>.csv` is named per repository. Move or rename the folder first if you want to keep earlier results.

> **Privacy:** these files contain commit messages, file paths, names and emails from the analysed repository. Don't commit or share them unless you're allowed to share that repository's contents.

## Limitations

- **It measures the shape of a change, not its impact.** Review the order before you use it.
- **It favours large commits.** Most rankings sort by size first, so someone who works in many small commits can rank lower than they deserve. It works best on repositories that squash-merge pull requests, where one commit is one pull request. Claude groups related commits to compensate, but check for important work that's missing.
- **STAR write-ups need checking.** Results git can't verify are left as `[placeholders]` for you to fill in. Check every claim before you use it.
- **It's a rough first pass.** The rankings, filters and composite weights were generated with an AI agent and haven't been tuned against real outcomes. They surface strong candidates, but you're encouraged to adjust them: see [Tuning for your stack](#tuning-for-your-stack).
- **There are no automated tests.** No evaluations (repeatable test scenarios) are included.

## Tuning for your stack

The patterns cover common tools and frameworks across Ruby, JavaScript/TypeScript, Python, Go, Rust, JVM, .NET, Swift, Dart and PHP. If your stack's commits are misclassified, edit these patterns:

| What's going wrong | Where to change it |
|---|---|
| Lint or format commits from your tools appear in the rankings | `MECHANICAL_TITLE_RE` in [scripts/rank_contributions.py](scripts/rank_contributions.py) |
| Framework or runtime upgrades aren't treated as high-stakes work | `PLATFORM_UPGRADE_RE` in [scripts/rank_contributions.py](scripts/rank_contributions.py) |
| Generated code, build output or lockfiles count as authored lines | `GENERATED_RE` and `LOCKFILES` in [scripts/git_contributions_csv.py](scripts/git_contributions_csv.py) |
| Dependency manifests, tests, migrations or CI files are misclassified | `DEP_MANIFESTS`, `TEST_RE`, `MIGRATION_RE` and `CI_INFRA_RE` in [scripts/git_contributions_csv.py](scripts/git_contributions_csv.py) |
| The overall ordering feels wrong | `RANKINGS` and `COMPOSITE_WEIGHTS` in [scripts/rank_contributions.py](scripts/rank_contributions.py) |

Avoid ambiguous words in the title patterns. For example, `spring` is both a Java framework and a Rails gem, so the upgrade pattern uses `spring boot` instead.

To check a change, run the analysis again and look at:
- `excluded.csv`, which lists every filtered-out commit and the reason
- the `high_stakes_infrastructure` ranking in `summary.md`

If you change the path patterns in `git_contributions_csv.py`, regenerate the CSV as well, because file categories are computed there.

## Using the scripts on their own

Both scripts work without Claude:

```bash
python3 scripts/git_contributions_csv.py -C /path/to/repo --author "you@example.com" --rev main -o out.csv
python3 scripts/rank_contributions.py out.csv --out-dir rankings --top 20
```

Run either one with `--help` to see all options.
