#!/usr/bin/env python3
"""
git_contributions_csv.py - export one CSV row per commit for a given author,
with attributes for judging what kind of change each commit is and how
significant it is next to the author's other commits.

Standard library only. Needs git >= 2.31 (for --diff-merges).

Usage:
  git_contributions_csv.py --author "jane@example.com" [options] > out.csv
  git_contributions_csv.py -C ~/src/repo --author Jane --author jdoe@old.com -o jane.csv

--author takes git's --author regex (matched against "Name <email>"), and you
can repeat it to cover several identities (they are OR'd). .mailmap is respected.

Message columns:
  title         first line of the raw commit message, exactly as stored
  body          everything after the first line, with only the one newline that
                ends the title line removed; the blank separator line and all
                other whitespace are kept as stored
  full_message  the full raw message (%B), unchanged
"""

import argparse
import bisect
import csv
import math
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime

FS = "\x1f"  # field separator inside a header record
REC = "\x01"  # marks the start of a commit in the diff streams

# --------------------------------------------------------------------------
# Path classification
# --------------------------------------------------------------------------

LOCKFILES = {
    "gemfile.lock", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
    "pnpm-lock.yaml", "bun.lockb", "poetry.lock", "pipfile.lock", "uv.lock",
    "cargo.lock", "go.sum", "composer.lock", "podfile.lock", "mix.lock",
    "packages.lock.json", "flake.lock", "gradle.lockfile", "pubspec.lock",
    "package.resolved", "deno.lock", "bun.lock", "gopkg.lock",
}
DEP_MANIFESTS = {
    "gemfile", "package.json", "requirements.txt", "pyproject.toml", "pipfile",
    "setup.py", "setup.cfg", "go.mod", "cargo.toml", "pom.xml", "build.gradle",
    "build.gradle.kts", "composer.json", "podfile", "mix.exs", ".tool-versions",
    ".ruby-version", ".node-version", ".nvmrc", ".python-version", "pubspec.yaml",
    "package.swift", "deno.json", "gopkg.toml", "directory.packages.props",
}
CI_INFRA_RE = re.compile(
    r"(^|/)(\.github/|\.gitlab-ci|\.circleci/|\.buildkite/|jenkinsfile|"
    r"azure-pipelines|\.travis\.yml|dockerfile|docker-compose|compose\.ya?ml|"
    r"procfile|terraform/|\.tf$|helm/|k8s/|kubernetes/|deploy/|\.drone\.yml|"
    r"bitbucket-pipelines|makefile$|capfile|config/deploy)",
    re.I,
)
TEST_RE = re.compile(
    r"(^|/)(test|tests|spec|specs|__tests__|__mocks__|testing|e2e|cypress|"
    r"features|fixtures|factories)/|"
    r"(_test|_spec|\.test|\.spec|-test|-spec|_tests)\.[^/]+$|(^|/)test_[^/]+\.py$|"
    r"(^|/)conftest\.py$",
    re.I,
)
DOCS_RE = re.compile(
    r"(^|/)(docs?|documentation)/|\.(md|mdx|rst|adoc|txt|rdoc)$|"
    r"(^|/)(readme|changelog|changes|license|licence|contributing|authors|history)[^/]*$",
    re.I,
)
DATA_RE = re.compile(
    r"(^|/)(fixtures?|__fixtures__|testdata|test_data|seeds?|sample_data|cassettes|vcr_cassettes)/|"
    r"\.(csv|tsv|sql|dump|ndjson|jsonl|parquet|xlsx?|har)$",
    re.I,
)
MIGRATION_RE = re.compile(r"(^|/)(db/migrate|migrations?|alembic/versions|flyway)/", re.I)
GENERATED_RE = re.compile(
    r"(^|/)(vendor|node_modules|dist|build|out|coverage|bower_components|"
    r"__snapshots__|__generated__|\.yarn|\.next|\.nuxt|target|obj|public/packs|public/assets|third_party)/|"
    r"\.(min\.js|min\.css|map|snap|pb\.go|lock)$|_pb2(_grpc)?\.pyi?$|\.generated\.|"
    r"_generated\.go$|\.g\.dart$|\.designer\.cs$|"
    r"(^|/)(db/schema\.rb|db/structure\.sql|schema\.graphql)$",
    re.I,
)
CONFIG_RE = re.compile(
    r"(^|/)(config|conf|settings|\.config)/|\.(ya?ml|toml|ini|cfg|conf|env|properties|"
    r"json|xml|plist)$|(^|/)\.[^/]*rc(\.[^/]+)?$|(^|/)\.(editorconfig|gitignore|"
    r"gitattributes|dockerignore|rubocop\.yml|eslintrc[^/]*|prettierrc[^/]*)$",
    re.I,
)

EXT_LANG = {
    "rb": "Ruby", "rake": "Ruby", "gemspec": "Ruby", "ru": "Ruby", "erb": "ERB",
    "haml": "Haml", "slim": "Slim", "py": "Python", "ipynb": "Jupyter",
    "js": "JavaScript", "mjs": "JavaScript", "cjs": "JavaScript", "jsx": "JavaScript",
    "ts": "TypeScript", "tsx": "TypeScript", "vue": "Vue", "svelte": "Svelte",
    "go": "Go", "rs": "Rust", "java": "Java", "kt": "Kotlin", "kts": "Kotlin",
    "scala": "Scala", "cs": "C#", "fs": "F#", "c": "C", "h": "C", "cc": "C++",
    "cpp": "C++", "hpp": "C++", "cxx": "C++", "m": "Objective-C", "mm": "Objective-C",
    "swift": "Swift", "php": "PHP", "ex": "Elixir", "exs": "Elixir", "erl": "Erlang",
    "clj": "Clojure", "hs": "Haskell", "lua": "Lua", "pl": "Perl", "r": "R",
    "dart": "Dart", "sql": "SQL", "sh": "Shell", "bash": "Shell", "zsh": "Shell",
    "ps1": "PowerShell", "html": "HTML", "htm": "HTML", "css": "CSS", "scss": "SCSS",
    "sass": "SCSS", "less": "Less", "graphql": "GraphQL", "gql": "GraphQL",
    "proto": "Protobuf", "tf": "Terraform", "hcl": "HCL", "yml": "YAML",
    "yaml": "YAML", "json": "JSON", "xml": "XML", "toml": "TOML", "md": "Markdown",
    "mdx": "Markdown", "rst": "reST", "txt": "Text", "csv": "CSV",
}


def classify(path):
    """Return the single primary category for a path (first match wins)."""
    base = os.path.basename(path).lower()
    if base in LOCKFILES:
        return "lockfile"
    if GENERATED_RE.search(path):
        return "generated"
    if DATA_RE.search(path) and not re.search(r"\.(rb|py|js|ts|go|java|ex|php)$", path, re.I):
        return "data"
    if TEST_RE.search(path):
        return "test"
    if MIGRATION_RE.search(path):
        return "migration"
    if base in DEP_MANIFESTS or base.endswith((".gemspec", ".csproj", ".fsproj", ".vbproj")) or re.match(r"requirements.*\.txt$", base):
        return "dependency"
    if CI_INFRA_RE.search(path):
        return "ci_infra"
    if DOCS_RE.search(path):
        return "docs"
    if CONFIG_RE.search(path):
        return "config"
    return "source"


CATEGORIES = ["source", "test", "docs", "config", "dependency", "lockfile",
              "generated", "data", "migration", "ci_infra"]
# Excluded from effective_* metrics: usually machine-produced or bulk data, not authored logic.
LOW_SIGNAL = {"lockfile", "generated", "data"}


def language(path):
    base = os.path.basename(path)
    if "." not in base:
        return {"Dockerfile": "Dockerfile", "Makefile": "Makefile", "Gemfile": "Ruby",
                "Rakefile": "Ruby", "Procfile": "Procfile"}.get(base, "")
    return EXT_LANG.get(base.rsplit(".", 1)[1].lower(), "")


def extension(path):
    base = os.path.basename(path)
    if "." not in base.lstrip("."):
        return "(none)"
    return base.rsplit(".", 1)[1].lower()


# --------------------------------------------------------------------------
# Message analysis
# --------------------------------------------------------------------------

CONVENTIONAL_RE = re.compile(r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?:\s")
TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
ISSUE_RE = re.compile(r"(?:^|[\s(\[])#(\d+)\b")
CLOSES_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+(?:[\w.-]+/[\w.-]+)?#\d+", re.I)
PR_MERGE_RE = re.compile(r"^Merge pull request #(\d+)")
# Merging the mainline into a topic branch: its first-parent diff is other people's work.
SYNC_MERGE_RE = re.compile(r"^Merge (remote-tracking )?branch '(origin/)?(master|main|develop|development|trunk|staging)'( of \S+)? into ", re.I)
PR_SQUASH_RE = re.compile(r"\(#(\d+)\)\s*$")
MR_RE = re.compile(r"See merge request [^\s]*!(\d+)")
REVERT_RE = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
TRAILER_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):\s+(.+)$")
URL_RE = re.compile(r"https?://\S+")

KEYWORD_CATEGORIES = [
    ("revert", re.compile(r"^revert\b", re.I)),
    ("merge", re.compile(r"^merge\b", re.I)),
    ("wip", re.compile(r"\b(wip|work in progress|tmp|temp commit)\b", re.I)),
    ("fixup", re.compile(r"^(fixup|squash|amend)!", re.I)),
    ("hotfix", re.compile(r"\bhot ?fix\b", re.I)),
    ("security", re.compile(r"\b(security|cve-\d+|vulnerab\w*|xss|csrf|injection|sanitiz\w*)\b", re.I)),
    ("dependency", re.compile(r"\b(bump|upgrade|downgrade|update[sd]?\s+\S+\s+(to|from)|dependenc\w*|deps?)\b", re.I)),
    ("bugfix", re.compile(r"\b(fix(e[sd])?|bug|issue|broken|crash|error|regression|patch|correct(s|ed)?)\b", re.I)),
    ("performance", re.compile(r"\b(perf(ormance)?|optimi[sz]\w*|speed ?up|faster|slow|cache|n\+1|latency)\b", re.I)),
    ("refactor", re.compile(r"\b(refactor\w*|clean ?up|restructur\w*|simplif\w*|extract\w*|rename\w*|reorgani[sz]\w*|tidy)\b", re.I)),
    ("test", re.compile(r"\b(tests?|specs?|coverage|flaky)\b", re.I)),
    ("docs", re.compile(r"\b(docs?|documentation|readme|comment(s|ed)?|typo)\b", re.I)),
    ("ci_build", re.compile(r"\b(ci|pipeline|build|docker|deploy\w*|workflow|lint\w*|rubocop|eslint)\b", re.I)),
    ("style", re.compile(r"\b(format\w*|whitespace|indent\w*|style)\b", re.I)),
    ("feature", re.compile(r"\b(add(s|ed)?|implement\w*|introduc\w*|support|new|create[sd]?|enable[sd]?|allow\w*)\b", re.I)),
    ("removal", re.compile(r"\b(remov\w*|delet\w*|drop\w*|deprecat\w*)\b", re.I)),
]
CONVENTIONAL_MAP = {
    "feat": "feature", "feature": "feature", "fix": "bugfix", "bugfix": "bugfix",
    "hotfix": "hotfix", "perf": "performance", "refactor": "refactor",
    "docs": "docs", "doc": "docs", "test": "test", "tests": "test",
    "build": "ci_build", "ci": "ci_build", "chore": "chore", "style": "style",
    "revert": "revert", "deps": "dependency", "security": "security",
}


def split_message(raw):
    """Split a raw message into (title, body) without changing any characters
    beyond the single newline that ends the title line."""
    if "\n" in raw:
        title, body = raw.split("\n", 1)
    else:
        title, body = raw, ""
    return title, body


def parse_trailers(raw):
    """Trailers = the final paragraph, if every non-empty line in it is Key: value."""
    paras = re.split(r"\n\s*\n", raw.strip())
    if len(paras) < 2:
        return []
    lines = [l for l in paras[-1].splitlines() if l.strip()]
    pairs = [TRAILER_RE.match(l) for l in lines]
    if lines and all(pairs):
        return [(m.group(1).lower(), m.group(2)) for m in pairs]
    return []


def message_category(title, conv_type, is_merge):
    if is_merge:
        return "merge"
    if conv_type and conv_type.lower() in CONVENTIONAL_MAP:
        return CONVENTIONAL_MAP[conv_type.lower()]
    for name, rx in KEYWORD_CATEGORIES:
        if rx.search(title):
            return name
    return "other"


# --------------------------------------------------------------------------
# git plumbing
# --------------------------------------------------------------------------

def git(repo, args, binary=False):
    cmd = ["git", "-C", repo, "-c", "core.quotepath=off", "--no-pager"] + args
    try:
        out = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    except subprocess.CalledProcessError as e:
        sys.exit("git failed: %s\n%s" % (" ".join(cmd), e.stderr.decode("utf-8", "replace")))
    return out if binary else out.decode("utf-8", "replace")


def git_stream(repo, args):
    cmd = ["git", "-C", repo, "-c", "core.quotepath=off", "--no-pager"] + args
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    for line in proc.stdout:
        yield line
    if proc.wait() != 0:
        sys.exit("git failed: %s" % " ".join(cmd))


def read_headers(repo, rev_args):
    fmt = FS.join(["%H", "%h", "%P", "%aN", "%aE", "%aI", "%cN", "%cE", "%cI", "%D", "%B"])
    out = git(repo, ["log", "-z", "--format=" + fmt] + rev_args)
    commits = []
    for rec in out.split("\0"):
        if not rec.strip():
            continue
        f = rec.split(FS, 10)
        if len(f) < 11:
            continue
        commits.append({
            "sha": f[0], "short_sha": f[1], "parents": f[2].split(),
            "author_name": f[3], "author_email": f[4], "author_date": f[5],
            "committer_name": f[6], "committer_email": f[7], "commit_date": f[8],
            "refs": f[9], "raw_message": f[10],
        })
    return commits


def read_file_changes(repo, rev_args, diff_args):
    """Return {sha: [ {path, old_path, status, added, deleted, binary, mode_old, mode_new} ]}."""
    out = git(repo, ["log", "-z", "--format=" + REC + "%H", "--raw", "--numstat"]
              + diff_args + rev_args)
    tokens = out.split("\0")
    result = {}
    i, n = 0, len(tokens)
    sha, raw_entries, num_entries = None, [], []

    def flush():
        if sha is None:
            return
        files = []
        for idx, r in enumerate(raw_entries):
            ns = num_entries[idx] if idx < len(num_entries) else ("0", "0")
            binary = ns[0] == "-"
            r.update(added=0 if binary else int(ns[0]), deleted=0 if binary else int(ns[1]), binary=binary)
            files.append(r)
        result[sha] = files

    while i < n:
        t = tokens[i].lstrip("\n")
        if t.startswith(REC):
            flush()
            sha, raw_entries, num_entries = t[1:], [], []
            i += 1
        elif t.startswith(":"):
            meta = t[1:].split(" ")
            status = meta[4] if len(meta) > 4 else "M"
            code = status[0]
            if code in "RC":
                old, new = tokens[i + 1], tokens[i + 2]
                i += 3
            else:
                old = new = tokens[i + 1]
                i += 2
            raw_entries.append({"path": new, "old_path": old, "status": code,
                                "similarity": int(status[1:]) if status[1:].isdigit() else "",
                                "mode_old": meta[0], "mode_new": meta[1] if len(meta) > 1 else ""})
        elif "\t" in t:
            parts = t.split("\t")
            if len(parts) >= 3 and parts[2] == "":
                i += 3  # rename/copy: old and new path follow as separate tokens
            else:
                i += 1
            num_entries.append((parts[0], parts[1]))
        else:
            i += 1
    flush()
    return result


def read_hunks(repo, rev_args, diff_args):
    """Count diff hunks per commit (number of distinct changed regions)."""
    counts = {}
    sha = None
    for line in git_stream(repo, ["log", "--format=" + REC + "%H", "-p", "-U0", "--no-color",
                                  "--no-ext-diff"] + diff_args + rev_args):
        if line.startswith(REC.encode()):
            sha = line[1:].strip().decode()
            counts[sha] = 0
        elif line.startswith(b"@@ ") and sha:
            counts[sha] += 1
    return counts


def read_history(repo, rev_scope, diff_args):
    """Whole history (all authors), oldest first: [(sha, author_email, unix_time, [paths])]."""
    out = git(repo, ["log", "--reverse", "-z", "--format=" + REC + "%H" + FS + "%aE" + FS + "%at",
                     "--name-only"] + diff_args + rev_scope)
    history = []
    cur = None
    for t in out.split("\0"):
        t = t.lstrip("\n")
        if t.startswith(REC):
            sha, email, ts = t[1:].split(FS)
            cur = (sha, email.lower(), int(ts), [])
            history.append(cur)
        elif t and cur is not None:
            cur[3].append(t)
    return history


def read_reverts(repo, rev_scope):
    """Map reverted sha -> list of reverting shas, across all authors."""
    out = git(repo, ["log", "-z", "--format=%H" + FS + "%B", "--grep=This reverts commit"] + rev_scope)
    reverted = defaultdict(list)
    for rec in out.split("\0"):
        if FS not in rec:
            continue
        sha, msg = rec.split(FS, 1)
        for target in REVERT_RE.findall(msg):
            reverted[target].append(sha.strip())
    return reverted


def read_tags(repo, shas):
    """First tag that contains each commit (approximates the release it shipped in)."""
    tags = {}
    for i in range(0, len(shas), 500):
        chunk = shas[i:i + 500]
        names = git(repo, ["name-rev", "--tags", "--name-only"] + chunk).splitlines()
        for sha, name in zip(chunk, names):
            name = name.strip()
            if name and name != "undefined":
                tags[sha] = re.sub(r"[~^].*$", "", name).replace("tags/", "")
    return tags


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def entropy(churns):
    """Normalized Shannon entropy of how churn is spread over files (0 = one file, 1 = even)."""
    total = sum(churns)
    if total <= 0 or len(churns) < 2:
        return 0.0
    h = -sum((c / total) * math.log2(c / total) for c in churns if c > 0)
    return round(h / math.log2(len(churns)), 4)


def percentile_ranks(values):
    s = sorted(values)
    n = len(s)
    return [round(100.0 * bisect.bisect_right(s, v) / n, 1) if n else 0 for v in values]


def iso_to_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-C", "--repo", default=".", help="path to the git repository (default: .)")
    ap.add_argument("-a", "--author", action="append", required=True,
                    help="author regex passed to git log --author; repeat for several identities")
    ap.add_argument("--rev", default="HEAD", help="revision/range to walk (default: HEAD)")
    ap.add_argument("--all", action="store_true", help="walk all refs instead of --rev")
    ap.add_argument("--since", help="only commits after this date (git date syntax)")
    ap.add_argument("--until", help="only commits before this date (git date syntax)")
    ap.add_argument("--no-merges", action="store_true", help="exclude merge commits")
    ap.add_argument("--no-hunks", action="store_true", help="skip hunk counting (faster on huge histories)")
    ap.add_argument("--no-history", action="store_true",
                    help="skip whole-history hotspot/ownership metrics (faster on huge repos)")
    ap.add_argument("--with-tags", action="store_true", help="add the first tag that contains each commit")
    ap.add_argument("--max-files-listed", type=int, default=200,
                    help="cap the number of paths in files_changed_list (default: 200, 0 = unlimited)")
    ap.add_argument("-o", "--output", help="output CSV path (default: stdout)")
    args = ap.parse_args()

    repo = args.repo
    git(repo, ["rev-parse", "--git-dir"])
    repo_name = os.path.basename(git(repo, ["rev-parse", "--show-toplevel"]).strip())

    scope = ["--all"] if args.all else [args.rev]
    rev_args = ["--use-mailmap"] + ["--author=" + a for a in args.author]
    if args.since:
        rev_args.append("--since=" + args.since)
    if args.until:
        rev_args.append("--until=" + args.until)
    if args.no_merges:
        rev_args.append("--no-merges")
    rev_args += scope + ["--"]
    # Merge commits are diffed against their first parent: the change the merge brought in.
    diff_args = ["-M", "--diff-merges=first-parent"]

    print("[1/5] reading commit metadata...", file=sys.stderr)
    commits = read_headers(repo, rev_args)
    if not commits:
        sys.exit("no commits matched author %s" % args.author)
    print("      %d commits" % len(commits), file=sys.stderr)

    print("[2/5] reading file-level changes...", file=sys.stderr)
    changes = read_file_changes(repo, rev_args, diff_args)

    hunks = {}
    if not args.no_hunks:
        print("[3/5] counting hunks...", file=sys.stderr)
        hunks = read_hunks(repo, rev_args, diff_args)

    # Whole-history pass: file hotspots, prior authors, author familiarity.
    prior = {}
    total_repo_commits = 0
    if not args.no_history:
        print("[4/5] scanning full history for hotspot/ownership metrics...", file=sys.stderr)
        author_emails = {c["author_email"].lower() for c in commits}
        history = read_history(repo, ["--use-mailmap"] + scope + ["--"], diff_args)
        total_repo_commits = len(history)
        touches = Counter()
        file_authors = defaultdict(set)
        author_files = set()
        repo_files_seen = set()
        authored_cache = {}

        def authored(path):
            if path not in authored_cache:
                authored_cache[path] = classify(path) not in LOW_SIGNAL
            return authored_cache[path]

        for idx, (sha, email, ts, paths) in enumerate(history):
            mine = email in author_emails
            prior[sha] = {
                "repo_commit_index": idx + 1,
                # Hotspot stats ignore lockfiles/generated/data (e.g. db/schema.rb is
                # touched by every migration and would make any commit look central).
                "touches": [touches[p] for p in paths if authored(p)],
                "authors": [len(file_authors[p] - {email}) for p in paths if authored(p)],
                "new_to_author": sum(1 for p in paths if p not in author_files and p in repo_files_seen),
                "new_to_repo": sum(1 for p in paths if p not in repo_files_seen),
            }
            for p in paths:
                touches[p] += 1
                file_authors[p].add(email)
                repo_files_seen.add(p)
                if mine:
                    author_files.add(p)

    print("[5/5] reverts%s and assembling rows..." % (" / tags" if args.with_tags else ""), file=sys.stderr)
    reverted_by = read_reverts(repo, scope + ["--"])
    tags = read_tags(repo, [c["sha"] for c in commits]) if args.with_tags else {}

    # Author-relative sequence (oldest first) for gaps between commits.
    chrono = sorted(commits, key=lambda c: iso_to_dt(c["author_date"]))
    prev_time = {}
    for i, c in enumerate(chrono):
        c["author_commit_index"] = i + 1
        prev_time[c["sha"]] = iso_to_dt(chrono[i - 1]["author_date"]) if i else None

    rows = []
    for c in commits:
        sha = c["sha"]
        raw = c["raw_message"]
        title, body = split_message(raw)
        files = changes.get(sha, [])
        is_merge = len(c["parents"]) > 1

        conv = CONVENTIONAL_RE.match(title)
        conv_type = conv.group("type") if conv else ""
        trailers = parse_trailers(raw)
        trailer_keys = [k for k, _ in trailers]
        coauthors = [v for k, v in trailers if k == "co-authored-by"]

        pr = ""
        for rx, src in ((PR_MERGE_RE, title), (PR_SQUASH_RE, title), (MR_RE, raw)):
            m = rx.search(src)
            if m:
                pr = m.group(1)
                break

        status_counts = Counter(f["status"] for f in files)
        cat_files = Counter()
        cat_churn = Counter()
        lang_churn = Counter()
        ext_counts = Counter()
        churns = []
        for f in files:
            cat = classify(f["path"])
            churn = f["added"] + f["deleted"]
            f["category"] = cat
            cat_files[cat] += 1
            cat_churn[cat] += churn
            churns.append(churn)
            lang = language(f["path"])
            if lang:
                lang_churn[lang] += churn or 1
            ext_counts[extension(f["path"])] += 1

        added = sum(f["added"] for f in files)
        deleted = sum(f["deleted"] for f in files)
        churn = added + deleted
        low_signal_churn = sum(cat_churn[k] for k in LOW_SIGNAL)
        effective = [f for f in files if f["category"] not in LOW_SIGNAL]
        eff_added = sum(f["added"] for f in effective)
        eff_deleted = sum(f["deleted"] for f in effective)
        largest = max(files, key=lambda f: f["added"] + f["deleted"], default=None)

        dirs = {os.path.dirname(f["path"]) or "." for f in files}
        top_dirs = sorted({f["path"].split("/", 1)[0] if "/" in f["path"] else "." for f in files})
        modules = {"/".join(f["path"].split("/")[:2]) if f["path"].count("/") >= 2 else os.path.dirname(f["path"]) or "."
                   for f in files}

        a_dt, c_dt = iso_to_dt(c["author_date"]), iso_to_dt(c["commit_date"])
        prev = prev_time.get(sha)

        paths = [f["path"] for f in files]
        if args.max_files_listed and len(paths) > args.max_files_listed:
            paths = paths[:args.max_files_listed] + ["...(+%d more)" % (len(files) - args.max_files_listed)]

        row = {
            "repo": repo_name,
            "sha": sha,
            "short_sha": c["short_sha"],
            "author_name": c["author_name"],
            "author_email": c["author_email"],
            "author_date": c["author_date"],
            "committer_name": c["committer_name"],
            "committer_email": c["committer_email"],
            "commit_date": c["commit_date"],
            "author_weekday": a_dt.strftime("%a"),
            "author_hour_local": a_dt.hour,
            "author_commit_index": c["author_commit_index"],
            "days_since_prev_author_commit": round((a_dt - prev).total_seconds() / 86400, 3) if prev else "",
            "commit_lag_hours": round((c_dt - a_dt).total_seconds() / 3600, 2),
            "committed_by_other": int(c["committer_email"].lower() != c["author_email"].lower()),
            # message (verbatim)
            "title": title,
            "body": body,
            "full_message": raw,
            "commit_title_length": len(title),
            "commit_body_length": len(body.strip()),
            "commit_message_length": len(raw),
            "body_line_count": len([l for l in body.splitlines() if l.strip()]),
            "body_bullet_count": len(re.findall(r"^\s*[-*•]\s+", body, re.M)),
            "body_url_count": len(URL_RE.findall(body)),
            # message semantics
            "conventional_type": conv_type,
            "conventional_scope": conv.group("scope") or "" if conv else "",
            "breaking_change": int(bool((conv and conv.group("bang")) or re.search(r"^BREAKING[ -]CHANGE", raw, re.M))),
            "message_category": message_category(title, conv_type, is_merge),
            "ticket_refs": ";".join(dict.fromkeys(TICKET_RE.findall(raw))),
            "issue_refs": ";".join(dict.fromkeys(ISSUE_RE.findall(raw))),
            "closes_issue": int(bool(CLOSES_RE.search(raw))),
            "pr_number": pr,
            "trailers": ";".join(dict.fromkeys(trailer_keys)),
            "co_author_count": len(coauthors),
            "co_authors": ";".join(coauthors),
            "signed_off": int("signed-off-by" in trailer_keys),
            "is_revert": int(bool(REVERT_RE.search(raw)) or title.lower().startswith("revert")),
            "reverts_sha": ";".join(REVERT_RE.findall(raw)),
            "was_reverted": int(any(sha.startswith(k) for k in reverted_by)) if reverted_by else 0,
            "reverted_by": ";".join(s for k, v in reverted_by.items() if sha.startswith(k) for s in v),
            "is_fixup": int(bool(re.match(r"^(fixup|squash|amend)!", title))),
            "is_wip": int(bool(re.search(r"\bwip\b", title, re.I))),
            # graph
            "is_merge": int(is_merge),
            "merge_kind": "" if not is_merge else ("pr_merge" if PR_MERGE_RE.match(title) or MR_RE.search(raw)
                                                   else "sync_merge" if SYNC_MERGE_RE.match(title)
                                                   else "branch_merge"),
            "parent_count": len(c["parents"]),
            "parent_shas": ";".join(c["parents"]),
            "is_root": int(not c["parents"]),
            "refs_at_commit": c["refs"],
            "first_containing_tag": tags.get(sha, ""),
            # size
            "files_changed": len(files),
            "lines_added": added,
            "lines_deleted": deleted,
            "churn": churn,
            "net_lines": added - deleted,
            "add_delete_ratio": round(added / deleted, 3) if deleted else "",
            "effective_files_changed": len(effective),
            "effective_lines_added": eff_added,
            "effective_lines_deleted": eff_deleted,
            "effective_churn": eff_added + eff_deleted,
            "low_signal_churn": low_signal_churn,
            "hunks": hunks.get(sha, ""),
            "largest_file": largest["path"] if largest else "",
            "largest_file_churn": (largest["added"] + largest["deleted"]) if largest else 0,
            "largest_file_share": round((largest["added"] + largest["deleted"]) / churn, 3) if churn and largest else "",
            "change_entropy": entropy(churns),
            # file operations
            "files_added": status_counts["A"],
            "files_deleted": status_counts["D"],
            "files_modified": status_counts["M"],
            "files_renamed": status_counts["R"],
            "files_copied": status_counts["C"],
            "files_type_changed": status_counts["T"],
            "pure_renames": sum(1 for f in files if f["status"] == "R" and f["similarity"] == 100),
            "binary_files": sum(1 for f in files if f["binary"]),
            "mode_changes": sum(1 for f in files if f["status"] == "M" and f["mode_old"] != f["mode_new"]),
            "submodule_changes": sum(1 for f in files if "160000" in (f["mode_old"], f["mode_new"])),
            # spread
            "directories_touched": len(dirs),
            "modules_touched": len(modules),
            "top_level_dirs": ";".join(top_dirs),
            "top_level_dir_count": len(top_dirs),
            "extensions": ";".join("%s:%d" % kv for kv in ext_counts.most_common()),
            "languages": ";".join(l for l, _ in lang_churn.most_common()),
            "primary_language": lang_churn.most_common(1)[0][0] if lang_churn else "",
            "files_changed_list": ";".join(paths),
        }
        # Per-category file counts and churn
        for cat in CATEGORIES:
            row["%s_files" % cat] = cat_files[cat]
            row["%s_churn" % cat] = cat_churn[cat]
        row["test_to_source_churn_ratio"] = round(cat_churn["test"] / cat_churn["source"], 3) if cat_churn["source"] else ""
        row["touches_tests"] = int(cat_files["test"] > 0)
        row["touches_migrations"] = int(cat_files["migration"] > 0)
        row["touches_dependencies"] = int(cat_files["dependency"] + cat_files["lockfile"] > 0)
        row["touches_ci_infra"] = int(cat_files["ci_infra"] > 0)
        row["docs_only"] = int(bool(files) and cat_files["docs"] == len(files))
        row["tests_only"] = int(bool(files) and cat_files["test"] == len(files))
        row["dominant_change_area"] = cat_churn.most_common(1)[0][0] if churn else (cat_files.most_common(1)[0][0] if files else "")

        # Whole-history context
        p = prior.get(sha)
        if p:
            t = p["touches"]
            row["repo_commit_index"] = p["repo_commit_index"]
            row["repo_history_position_pct"] = round(100.0 * p["repo_commit_index"] / total_repo_commits, 1)
            row["files_prior_touches_mean"] = round(sum(t) / len(t), 2) if t else 0
            row["files_prior_touches_max"] = max(t) if t else 0
            row["files_other_authors_mean"] = round(sum(p["authors"]) / len(p["authors"]), 2) if p["authors"] else 0
            row["files_new_to_repo"] = p["new_to_repo"]
            row["files_new_to_author"] = p["new_to_author"]
        else:
            for k in ("repo_commit_index", "repo_history_position_pct", "files_prior_touches_mean",
                      "files_prior_touches_max", "files_other_authors_mean", "files_new_to_repo",
                      "files_new_to_author"):
                row[k] = ""
        rows.append(row)

    # Percentiles among this author's commits (100 = largest).
    for key in ("churn", "effective_churn", "files_changed", "hunks", "change_entropy", "commit_body_length"):
        vals = [r[key] if isinstance(r[key], (int, float)) else 0 for r in rows]
        for r, pct in zip(rows, percentile_ranks(vals)):
            r[key + "_pctile"] = pct

    if args.output and os.path.dirname(args.output):
        os.makedirs(os.path.dirname(args.output), exist_ok=True)
    out = open(args.output, "w", newline="", encoding="utf-8") if args.output else sys.stdout
    writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()), quoting=csv.QUOTE_MINIMAL)
    writer.writeheader()
    writer.writerows(rows)
    if args.output:
        out.close()
        print("wrote %d rows to %s" % (len(rows), args.output), file=sys.stderr)


if __name__ == "__main__":
    main()
