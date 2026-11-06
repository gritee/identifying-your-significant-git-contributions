#!/usr/bin/env python3
"""
rank_contributions.py - stack-rank commits from a git_contributions_csv.py export
by several different measures of "significance of contribution".

Usage:
  rank_contributions.py myrepo.csv
  rank_contributions.py myrepo.csv --top 50 --out-dir rankings/ --include-merges

Steps:
  1. Filter out noise (merges, WIP/fixup, reverted, rebase duplicates, mechanical
     sweeps). Each rule can be turned off and is reported with its count.
  2. Recompute percentiles on the filtered set.
  3. Produce one ranking per measure of significance (RANKINGS below), plus a
     composite score and a list of commits that show up in many rankings.

Writes into --out-dir:
  filtered.csv           the filtered commits with derived columns
  excluded.csv           the removed commits and the reason for each
  ranking_<name>.csv     one file per ranking (top N)
  composite.csv          all filtered commits ordered by composite score
  appearances.csv        commits ordered by how many top-N lists they appear in
  summary.md             every ranking's top N in one readable document
"""

import argparse
import os
import re
import sys

try:
    import pandas as pd
except ImportError:
    sys.exit("rank_contributions.py needs pandas: install it with `%s -m pip install pandas`" % sys.executable)

# --------------------------------------------------------------------------
# Rankings: (name, description, filter(df) -> mask or None, [(column, ascending)])
# Sort keys are applied in order; ties fall through to the next key.
# --------------------------------------------------------------------------

def _in(col, values):
    return lambda d: d[col].isin(values)


RANKINGS = [
    ("authored_volume",
     "Size of authored change, excluding lockfiles, generated files and data/fixtures",
     None,
     [("effective_churn", False), ("hunks", False), ("effective_files_changed", False)]),

    ("new_functionality",
     "Feature commits that added capability (new files, new lines of source)",
     _in("message_category", ["feature"]),
     [("files_added", False), ("effective_lines_added", False), ("source_churn", False)]),

    ("architectural_reach",
     "Substantial changes spread evenly across many modules",
     lambda d: d["effective_churn"] >= d.attrs["min_churn"],
     [("modules_touched", False), ("change_entropy", False), ("top_level_dir_count", False)]),

    ("core_code_impact",
     "Changes to files the rest of the team edits most",
     lambda d: d["source_churn"] > 0,
     [("files_prior_touches_mean", False), ("files_other_authors_mean", False), ("source_churn", False)]),

    ("greenfield",
     "Commits that created new parts of the codebase",
     lambda d: d["files_new_to_repo"] > 0,
     [("files_new_to_repo", False), ("effective_lines_added", False)]),

    ("expanding_into_unfamiliar_code",
     "Changes to existing files the author had never touched before",
     lambda d: d["files_new_to_author"] > 0,
     [("files_new_to_author", False), ("effective_churn", False)]),

    ("cleanup_simplification",
     "Refactors and removals that shrank the codebase",
     lambda d: d["message_category"].isin(["refactor", "removal"]) | (d["effective_net_lines"] < -d.attrs["min_churn"]),
     [("effective_net_lines", True), ("files_deleted", False), ("effective_lines_deleted", False)]),

    ("high_stakes_infrastructure",
     "Breaking changes, schema migrations, dependency/framework upgrades, CI/deploy",
     lambda d: d["stakes_areas"] > 0,
     [("breaking_change", False), ("is_platform_upgrade", False), ("stakes_areas", False),
      ("effective_churn", False)]),

    ("bugfixes_in_core_code",
     "Bug, hot and security fixes, weighted toward heavily used files",
     _in("message_category", ["bugfix", "hotfix", "security"]),
     [("files_prior_touches_mean", False), ("hunks", False)]),

    ("tested_changes",
     "Source changes that came with substantial tests",
     lambda d: (d["source_churn"] >= 20) & (d["test_churn"] > 0),
     [("test_churn", False), ("test_to_source_churn_ratio", False)]),

    ("well_documented",
     "Commits with thorough write-ups and links to tickets or PRs",
     lambda d: d["commit_body_length"] > 0,
     [("commit_body_length", False), ("body_bullet_count", False), ("has_reference", False)]),
]

# Composite score: weighted mean of percentiles recomputed on the filtered set.
COMPOSITE_WEIGHTS = {
    "effective_churn": 1.0,
    "hunks": 1.0,
    "effective_files_changed": 0.5,
    "change_entropy": 0.5,
    "modules_touched": 0.5,
    "files_prior_touches_mean": 1.0,
    "files_new_to_repo": 0.5,
    "commit_body_length": 0.5,
}

DISPLAY_COLS = ["short_sha", "author_date", "message_category", "title"]

# Linters and formatters across common stacks. To tune for your stack, add your tools here.
MECHANICAL_TITLE_RE = re.compile(
    r"\b(?:lint(?:ing|er)?|format(?:ting)?|whitespace|reformat|autocorrect|autofix|bump version|"
    r"pre-commit|"
    r"rubocop|standardrb|"                                  # Ruby
    r"prettier|eslint|biome|stylelint|"                     # JavaScript / TypeScript / CSS
    r"black|isort|ruff|flake8|pylint|autopep8|"             # Python
    r"gofmt|goimports|golangci-lint|"                       # Go
    r"rustfmt|clippy|"                                      # Rust
    r"ktlint|spotless|checkstyle|"                          # JVM
    r"swiftlint|swiftformat|"                               # Swift
    r"clang-format|"                                        # C / C++
    r"php-cs-fixer|phpcs|"                                  # PHP
    r"dotnet format)\b", re.I)
# Non-merge commits that are really merge bookkeeping (conflict resolution, syncing master).
MERGE_LIKE_TITLE_RE = re.compile(
    r"\b(?:merge conflicts?|resolve(?:d)? conflicts?|conflict resolution|"
    r"merge (?:latest |in )?(?:origin/)?(?:master|main|develop))\b|^merge\b", re.I)
# Language/framework/runtime/platform upgrades.
# Upgrades of a language, framework, runtime or platform. To tune for your stack, add yours here.
# Leave out ambiguous words (e.g. "spring" is also a Rails gem, "next" is a common word).
PLATFORM_UPGRADE_RE = re.compile(
    r"\b(?:upgrade|bump|update|migrate)\b.*(?:\b(?:"
    r"framework|major version|image|"
    r"ruby|rails|"
    r"node(?:js)?|typescript|react|angular|vue|nuxt|next\.?js|svelte|webpack|vite|"
    r"python|django|flask|fastapi|"
    r"java|jdk|kotlin|scala|gradle|maven|spring boot|"
    r"go|golang|rust|elixir|phoenix|erlang|"
    r"php|laravel|symfony|dotnet|swift|xcode|ios|android|flutter|dart|"
    r"postgres(?:ql)?|mysql|mariadb|mongodb|redis|elasticsearch|"
    r"debian|ubuntu|alpine|kubernetes|terraform"
    r")\b|\.net\b)", re.I)


# --------------------------------------------------------------------------

def load(path):
    df = pd.read_csv(path, dtype={"sha": str, "short_sha": str, "pr_number": str,
                                  "ticket_refs": str, "issue_refs": str},
                     keep_default_na=False, na_values=[""])
    df["author_date"] = pd.to_datetime(df["author_date"], utc=True)
    for col in ("title", "body", "full_message", "ticket_refs", "pr_number", "issue_refs"):
        if col in df:
            df[col] = df[col].fillna("")
    return df


def derive(df):
    # Build the derived columns separately and attach them in one step: adding
    # them one at a time to a ~125-column frame triggers pandas PerformanceWarnings.
    new = {}
    new["has_reference"] = ((df["ticket_refs"] != "") | (df["pr_number"] != "")
                            | (df["issue_refs"] != "")).astype(int)
    files = df["files_changed"].clip(lower=1)
    ratio = df["add_delete_ratio"]
    new["churn_per_file"] = (df["effective_churn"] / files).round(1)
    new["churn_per_hunk"] = (df["effective_churn"] / df["hunks"].clip(lower=1)).round(1)
    new["effective_net_lines"] = df["effective_lines_added"] - df["effective_lines_deleted"]
    new["is_merge_like"] = df["title"].str.contains(MERGE_LIKE_TITLE_RE).astype(int)
    new["is_platform_upgrade"] = df["title"].str.contains(PLATFORM_UPGRADE_RE).astype(int)
    new["stakes_areas"] = ((df["breaking_change"] > 0).astype(int) + (df["migration_churn"] > 0)
                           + ((df["dependency_churn"] + df["lockfile_churn"]) > 0)
                           + (df["ci_infra_churn"] > 0) + new["is_platform_upgrade"]).astype(int)
    new["is_mechanical"] = (
        (df["message_category"] == "style")
        | df["title"].str.contains(MECHANICAL_TITLE_RE)
        # Many files, balanced adds/deletes, few lines per file: a find-and-replace sweep.
        | ((df["files_changed"] >= 25) & ratio.between(0.8, 1.25) & (new["churn_per_file"] <= 12))
        # Nothing but renames.
        | ((df["files_changed"] > 0) & (df["pure_renames"] == df["files_changed"]))
    ).astype(int)
    return pd.concat([df, pd.DataFrame(new, index=df.index)], axis=1)


def apply_filters(df, args):
    reasons = pd.Series("", index=df.index)

    def mark(mask, reason):
        nonlocal reasons
        mask = mask & (reasons == "")
        reasons[mask] = reason

    if not args.include_merges:
        mark(df["is_merge"] == 1, "merge")
    if not args.include_wip:
        mark((df["is_wip"] == 1) | (df["is_fixup"] == 1), "wip_or_fixup")
    if not args.include_merges:
        mark(df["is_merge_like"] == 1, "merge_bookkeeping")
    if not args.include_reverted:
        mark(df["was_reverted"] == 1, "reverted_later")
        mark(df["is_revert"] == 1, "is_revert")
    if not args.include_mechanical:
        mark(df["is_mechanical"] == 1, "mechanical_sweep")
    if not args.include_empty:
        mark(df["files_changed"] == 0, "empty_commit")
    if not args.keep_duplicates:
        # Same change recorded more than once (rebased/cherry-picked): same title and
        # author timestamp, sizes may differ slightly after conflict resolution. Keep the newest.
        dup = df.duplicated(subset=["title", "author_date"], keep="first")
        mark(dup, "duplicate")

    excluded = df[reasons != ""].assign(exclusion_reason=reasons[reasons != ""])
    kept = df[reasons == ""].copy()
    return kept, excluded


def add_percentiles(df):
    for col in COMPOSITE_WEIGHTS:
        df[col + "_fpctile"] = (df[col].fillna(0).rank(pct=True, method="max") * 100).round(1)
    total = sum(COMPOSITE_WEIGHTS.values())
    df["composite_score"] = sum(df[c + "_fpctile"] * w for c, w in COMPOSITE_WEIGHTS.items()) / total
    df["composite_score"] = df["composite_score"].round(2)
    df["composite_rank"] = df["composite_score"].rank(ascending=False, method="min").astype(int)
    return df


def rank(df, name, flt, keys, top):
    sub = df if flt is None else df[flt(df).fillna(False)]
    cols = [c for c, _ in keys]
    out = sub.sort_values(cols, ascending=[a for _, a in keys], na_position="last", kind="mergesort")
    out = out.head(top).copy()
    out.insert(0, "rank", range(1, len(out) + 1))
    shown = ["rank"] + DISPLAY_COLS + [c for c in cols if c not in DISPLAY_COLS] + ["composite_score", "sha"]
    return out[shown], len(sub)


def to_markdown(frame):
    def cell(v):
        if isinstance(v, pd.Timestamp):
            return v.strftime("%Y-%m-%d")
        if isinstance(v, float):
            return "" if pd.isna(v) else ("%g" % v)
        return str(v).replace("|", "\\|").replace("\n", " ")
    cols = [c for c in frame.columns if c != "sha"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in frame.iterrows():
        lines.append("| " + " | ".join(cell(r[c]) for c in cols) + " |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", help="CSV produced by git_contributions_csv.py")
    ap.add_argument("--out-dir", help="output directory (default: <csv name>_rankings next to the CSV)")
    ap.add_argument("--top", type=int, default=20, help="rows per ranking (default: 20)")
    ap.add_argument("--min-churn", type=int, default=100,
                    help="minimum effective_churn for the reach ranking and net-deletion cleanup (default: 100)")
    ap.add_argument("--since", help="only commits authored on/after this date (YYYY-MM-DD)")
    ap.add_argument("--until", help="only commits authored before this date (YYYY-MM-DD)")
    ap.add_argument("--include-merges", action="store_true",
                    help="keep merge commits and merge-bookkeeping commits (conflict fixes, syncing master)")
    ap.add_argument("--include-wip", action="store_true", help="keep WIP and fixup!/squash! commits")
    ap.add_argument("--include-reverted", action="store_true", help="keep reverts and commits later reverted")
    ap.add_argument("--include-mechanical", action="store_true", help="keep lint/format/rename sweeps")
    ap.add_argument("--include-empty", action="store_true", help="keep commits with no file changes")
    ap.add_argument("--keep-duplicates", action="store_true", help="keep rebased/cherry-picked copies")
    ap.add_argument("--quiet", action="store_true", help="don't print rankings to the terminal")
    args = ap.parse_args()

    df = derive(load(args.csv))
    if args.since:
        df = df[df["author_date"] >= pd.Timestamp(args.since, tz="UTC")]
    if args.until:
        df = df[df["author_date"] < pd.Timestamp(args.until, tz="UTC")]
    total = len(df)

    kept, excluded = apply_filters(df, args)
    if kept.empty:
        sys.exit("no commits left after filtering")
    kept = add_percentiles(kept)
    kept.attrs["min_churn"] = args.min_churn

    out_dir = args.out_dir or os.path.splitext(args.csv)[0] + "_rankings"
    os.makedirs(out_dir, exist_ok=True)
    kept.to_csv(os.path.join(out_dir, "filtered.csv"), index=False)
    excluded.to_csv(os.path.join(out_dir, "excluded.csv"), index=False)

    md = ["# Contribution rankings", "",
          "Source: `%s`  " % os.path.basename(args.csv),
          "Commits: %d total, %d kept, %d excluded" % (total, len(kept), len(excluded)), ""]
    if len(excluded):
        md += ["| exclusion reason | commits |", "|---|---|"]
        md += ["| %s | %d |" % kv for kv in excluded["exclusion_reason"].value_counts().items()]
        md.append("")

    appearances = pd.Series(0, index=kept.index)
    in_lists = {i: [] for i in kept.index}
    for name, desc, flt, keys in RANKINGS:
        table, eligible = rank(kept, name, flt, keys, args.top)
        table.to_csv(os.path.join(out_dir, "ranking_%s.csv" % name), index=False)
        for idx in table.index:
            appearances[idx] += 1
            in_lists[idx].append(name)
        sort_desc = " → ".join("%s %s" % (c, "↑" if a else "↓") for c, a in keys)
        md += ["## %s" % name, "", desc + ".  ", "Sort: %s  " % sort_desc,
               "Eligible commits: %d" % eligible, "", to_markdown(table), ""]
        if not args.quiet:
            print("\n== %s (%d eligible) ==" % (name, eligible))
            print(table.drop(columns=["sha"]).head(10).to_string(index=False, max_colwidth=60))

    composite = kept.sort_values("composite_score", ascending=False)
    comp_cols = ["composite_rank"] + DISPLAY_COLS + ["composite_score"] + \
                [c + "_fpctile" for c in COMPOSITE_WEIGHTS] + ["sha"]
    composite[comp_cols].to_csv(os.path.join(out_dir, "composite.csv"), index=False)
    md += ["## composite", "", "Weighted mean of percentiles: " +
           ", ".join("%s ×%g" % kv for kv in COMPOSITE_WEIGHTS.items()), "",
           to_markdown(composite[comp_cols].head(args.top)), ""]

    kept["ranking_appearances"] = appearances
    kept["rankings"] = [";".join(in_lists[i]) for i in kept.index]
    multi = kept[kept["ranking_appearances"] > 0].sort_values(
        ["ranking_appearances", "composite_score"], ascending=[False, False])
    app_cols = DISPLAY_COLS + ["ranking_appearances", "composite_score", "rankings", "sha"]
    multi[app_cols].to_csv(os.path.join(out_dir, "appearances.csv"), index=False)
    md += ["## appearances", "", "Commits in the most top-%d lists." % args.top, "",
           to_markdown(multi[app_cols].head(args.top)), ""]

    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    if not args.quiet:
        print("\n== composite ==")
        print(composite[["composite_rank", "short_sha", "title", "composite_score"]]
              .head(10).to_string(index=False, max_colwidth=60))
        print("\n== most appearances ==")
        print(multi[["short_sha", "title", "ranking_appearances", "rankings"]]
              .head(10).to_string(index=False, max_colwidth=60))
    print("\n%d of %d commits kept; wrote rankings to %s/" % (len(kept), total, out_dir), file=sys.stderr)


if __name__ == "__main__":
    main()
