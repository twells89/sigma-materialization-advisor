#!/bin/sh
# Block text that matches a private denylist (customer names, people, internal tools).
# The denylist lives OUTSIDE the repo so the list itself is never published:
#   ${SPA_DENYLIST:-$HOME/.config/sigma-performance-advisor/denylist.txt}
# One case-insensitive term per line; blank lines and lines starting with # are ignored.
#
#   check-denylist.sh --staged        # added lines + paths in the index (pre-commit)
#   check-denylist.sh --file <path>   # a commit message or PR body
#   check-denylist.sh --tree          # every tracked file and path
list="${SPA_DENYLIST:-$HOME/.config/sigma-performance-advisor/denylist.txt}"
if [ ! -f "$list" ]; then
  echo "denylist: $list not found; skipping customer-info check" >&2
  exit 0
fi
terms=$(mktemp) && trap 'rm -f "$terms"' EXIT
# CRLF lists and padded terms must still match: drop CRs, trim, skip comments and blanks
tr -d '\r' < "$list" | grep -v '^[[:space:]]*#' |
  sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e '/^$/d' > "$terms"
[ -s "$terms" ] || exit 0

case "$1" in
  --staged) text=$( { git diff --cached --name-only
                      git diff --cached --no-color --no-ext-diff -U0 \
                        --src-prefix=a/ --dst-prefix=b/ | grep '^+' |
                        grep -v -e '^+++ ["ab/]' -e '^+++ /dev/null'; } ) ;;
  --file)   text=$(cat "$2") ;;
  --tree)   text=$( { git ls-files; git grep --no-color -h -I -e '' -- . 2>/dev/null; } ) ;;
  *) echo "usage: $0 --staged | --file <path> | --tree" >&2; exit 2 ;;
esac

hits=$(printf '%s\n' "$text" | grep -i -o -F -f "$terms" | sort -fu)
if [ -n "$hits" ]; then
  echo "denylist: blocked — found term(s) from $list:" >&2
  printf '  %s\n' "$hits" >&2
  exit 1
fi
