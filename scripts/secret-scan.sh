#!/usr/bin/env bash
# secret-scan.sh — the pre-commit / pre-push leak scan.
#
#   scripts/secret-scan.sh                 # scan staged diff (default)
#   scripts/secret-scan.sh --staged
#   scripts/secret-scan.sh --range origin/main..HEAD   # every commit in the range (git log -p)
#   scripts/secret-scan.sh --range HEAD                # ALL history — what a first push sends
#   scripts/secret-scan.sh --worktree      # scan working-tree tracked files
#   scripts/secret-scan.sh --all           # scan every tracked file (slow, thorough)
#   scripts/secret-scan.sh path/to/file ...
#
# Exit 0 = clean, 1 = hits found (inspect every hit; most are false positives,
# but the ones that aren't are a rotation-and-rewrite incident, not a delete),
# 2 = the scan could not run (bad range, not a repo). Never "clean" by accident:
# until 2026-11-10 a missing origin/main made `--range` scan nothing and exit 0.
#
# Patterns are the house list: credentials, private keys, LAN/internal topology,
# and real-world identifiers that must never appear in a tracked file.
set -uo pipefail

PATTERNS=(
  # --- credentials -------------------------------------------------------
  'gh[pousr]_[A-Za-z0-9]{20,}'                       # GitHub tokens
  'AKIA[0-9A-Z]{16}'                                 # AWS access key id
  'aws_secret_access_key[[:space:]]*=[[:space:]]*[A-Za-z0-9/+=]{30,}'
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
  'xox[baprs]-[A-Za-z0-9-]{10,}'                     # Slack
  'sk-[A-Za-z0-9]{24,}'                              # OpenAI-style keys
  'hf_[A-Za-z0-9]{20,}'                              # Hugging Face
  '(AIza[0-9A-Za-z_-]{30,})'                         # Google API key
  '[A-Za-z0-9_-]{32,}\.apps\.googleusercontent\.com' # Google client id
  'Authorization:[[:space:]]*(Bearer|Basic)[[:space:]]+[A-Za-z0-9._+/=-]{12,}'
  '(eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,})'  # JWT
  '(password|passwd|secret|token|api_key|apikey)[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"' ]{8,}["'"'"']'
  # --- internal topology -------------------------------------------------
  '\b(10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|192\.168\.[0-9]{1,3}\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3})\b'
  # real machine names are NOT listed here: this script is copied into every seeded
  # project, REAL ones included, so it must stay abstract itself (until 2026-11-10 it
  # named the house boxes and would have shipped them with the first REAL push).
  # They load from the local-only internal-names file below.
  '/home/[a-z][a-z0-9_-]+/'
  '\bC:[\\/]+Users[\\/]'
  '\b[a-z0-9-]+\.internal\b'
  # --- infra identifiers -------------------------------------------------
  '\bE[A-Z0-9]{13}\b'                                # CloudFront distribution id
  '\bZ[A-Z0-9]{13,14}\b'                             # Route 53 hosted zone id
  '\b[0-9]{12}\.aws\.amazonaws\.com'                 # account-id URLs
  # --- model-origin canaries (AI-written code leaving the building) -----------
  # shapes an agent-authored file should never contain unasked; see
  # skills/agent-egress-defense. Generic cloud domains only — the known-bad
  # host list lives in a local-only file loaded below, never in this script.
  '\b169\.254\.169\.254\b|\b169\.254\.170\.2\b|\b100\.100\.100\.200\b|metadata\.google\.internal'
  'X-Amz-Signature|OSSAccessKeyId|x-oss-signature|q-sign-algorithm'
  '\b[a-z0-9-]+\.oss-[a-z0-9-]+\.aliyuncs\.com'      # Alibaba OSS bucket hosts
)

# Optional local-only pattern files: one ERE per line, merged into the list above.
# On Nick's boxes they add the known-bad agent-exfil hosts and the real internal
# machine names; elsewhere the scan simply runs the static list.
WATCHLIST="${META_WATCHLIST:-$HOME/meta/global/egress-watchlist.txt}"
INTERNAL="${META_INTERNAL_NAMES:-$HOME/meta/global/internal-names.txt}"
for list in "$WATCHLIST" "$INTERNAL"; do
  [ -f "$list" ] || continue
  while IFS= read -r line; do
    line="${line%%#*}"; line="${line// /}"
    [ -n "$line" ] && PATTERNS+=("$line")
  done < "$list"
done
# The scanner's own pattern list matches itself; never let that block a push.
SELF=(-- . ':(exclude)scripts/secret-scan.sh')

mode="staged"; target=""
case "${1:-}" in
  --staged)   mode="staged"; shift ;;
  --worktree) mode="worktree"; shift ;;
  --all)      mode="all"; shift ;;
  --range)    mode="range"; target="${2:-}"; shift 2 ;;
esac

PAT="$(IFS='|'; echo "${PATTERNS[*]}")"
# Placeholder filter, applied to the MATCHED TEXT, not the whole line: a line is
# dropped only when every match on it is itself a placeholder. Line-level filtering
# hid every hit on any line containing "<…>" (all of HTML) or the word "example".
PLACEHOLDER='(example|placeholder|changeme|change-me|put-me-in-env|<[^>]*>|\bX{3,}\b|your[-_]|dummy|redacted)'

scan() {  # $1 = label, $2 = max OK exit code of the command (grep: 1 = no match), rest = command
  local label="$1" okrc="$2"; shift 2
  local out rc hits="" line m keep
  out=$("$@" 2>/tmp/secret-scan.err.$$); rc=$?
  if [ "$rc" -gt "$okrc" ]; then
    echo "secret-scan: ERROR — could not scan $label (exit $rc): $(head -c 300 /tmp/secret-scan.err.$$)" >&2
    rm -f /tmp/secret-scan.err.$$; errored=1; return
  fi
  rm -f /tmp/secret-scan.err.$$
  while IFS= read -r line; do
    keep=0
    while IFS= read -r m; do
      [ -n "$m" ] && ! printf '%s' "$m" | grep -qiE -- "$PLACEHOLDER" && { keep=1; break; }
    done < <(printf '%s\n' "$line" | grep -oE -- "$PAT")
    [ "$keep" = 1 ] && hits+="$line"$'\n'
  done < <(printf '%s\n' "$out" | grep -nE -- "$PAT")
  hits="${hits%$'\n'}"
  if [ -n "$hits" ]; then
    echo "== $label: $(printf '%s\n' "$hits" | wc -l) possible hit(s) =="
    printf '%s\n' "$hits"
    found=1
  fi
}

# Diff modes judge ADDED lines only: what leaves the box. A commit that deletes a
# leaked key must be pushable (the rotation is the real fix; see the footer).
added() { set -o pipefail; "$@" | { grep -E '^\+' | grep -vE '^\+\+\+ '; true; }; }

found=0; errored=0
case "$mode" in
  staged)   scan "staged diff"   0 added git diff --cached -U0 "${SELF[@]}" ;;
  range)    [ -n "$target" ] || { echo "secret-scan: --range needs a revision range" >&2; exit 2; }
            read -ra revs <<< "$target"   # "A..B" or "SHA --not --remotes=origin"
            scan "$target"       0 added git log -p -U0 --format= "${revs[@]}" "${SELF[@]}" ;;
  worktree) scan "tracked files" 1 git grep -nE -e "$PAT" "${SELF[@]}" ;;
  all)      scan "all tracked"   1 git grep -nE -e "$PAT" "${SELF[@]}" ;;
esac

# explicit file arguments always get scanned too
for f in "$@"; do
  [ -f "$f" ] && scan "$f" 1 grep -nE -- "$PAT" "$f"
done

if [ "$found" = 1 ]; then
  cat >&2 <<'EOF'

secret-scan: inspect every hit above.
  Real secret committed          -> STOP, tell Nick NOW; rotate + rewrite history.
  Real internal detail committed -> remove it and reword the doc abstractly.
  False positive                 -> reword the example (placeholders: <host>, example.com,
                                    /srv/media, change-me) rather than adding a pattern skip.
EOF
  exit 1
fi
[ "$errored" = 1 ] && { echo "secret-scan: NOT clean — the scan itself failed (see above)" >&2; exit 2; }
echo "secret-scan: clean"
