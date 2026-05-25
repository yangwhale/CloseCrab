#!/bin/bash
# verify-page-urls.sh — Extract and HEAD-verify all http(s) URLs in an HTML file.
# For failed GitHub repo URLs, automatically probe common naming variants
# (hyphen add/remove, trailing 'o', underscore<->hyphen) and suggest the
# correct repo if found.
#
# Usage:  verify-page-urls.sh <html-file>
# Exit:   0 = all URLs PASS, 1 = some failed, 2 = bad input

set -u

HTML="${1:-}"
[ -n "$HTML" ]      || { echo "Usage: $0 <html-file>" >&2; exit 2; }
[ -f "$HTML" ]      || { echo "ERROR: $HTML not found" >&2; exit 2; }

# Build a context map: URL → list of HTML attribute contexts where it appears.
# We need this to know if a URL is preconnect / dns-prefetch (skip), og:image
# (warn-only), or a real user-facing href/src (fail on 4xx/5xx).
ALL_URLS=$(grep -oE 'https?://[^"<>[:space:])]+' "$HTML" | sort -u)
[ -n "$ALL_URLS" ] || { echo "(no URLs found in $HTML)"; exit 0; }

# Classify each URL by HTML context
classify_url() {
  local u="$1"
  # quote the URL for grep — use fixed-string match
  local matches
  matches=$(grep -F "$u" "$HTML" || true)

  # preconnect / dns-prefetch — typically <link rel="preconnect" href="...">
  if printf '%s\n' "$matches" | grep -qiE 'rel="?(preconnect|dns-prefetch|preload)"?[^>]*'"$(printf '%s' "$u" | sed 's/[\&/]/\\&/g')"; then
    echo "preconnect"; return
  fi
  if printf '%s\n' "$matches" | grep -qiE "$(printf '%s' "$u" | sed 's/[\&/]/\\&/g')"'[^>]*rel="?(preconnect|dns-prefetch|preload)"?'; then
    echo "preconnect"; return
  fi
  # og:image / twitter:image — social card meta tags
  if printf '%s\n' "$matches" | grep -qiE '(og:image|twitter:image)'; then
    echo "social-meta"; return
  fi
  # everything else: real content link
  echo "content"
}

echo "=== Verifying URLs in $(basename "$HTML") ==="

PASS=0
FAIL=0
SKIP=0
WARN=0
FAILED_URLS=()

while IFS= read -r url; do
  # Strip trailing punctuation common in HTML/Markdown context
  clean=$(printf '%s' "$url" | sed -E 's/[.,;:)\>\]\}"'"'"']+$//')
  ctx=$(classify_url "$clean")
  case "$ctx" in
    preconnect)
      printf "  \033[90m─\033[0m  SKIP  %s  (preconnect/dns-prefetch)\n" "$clean"
      SKIP=$((SKIP+1))
      continue
      ;;
  esac

  STATUS=$(curl -sSo /dev/null -w "%{http_code}" -m 10 -L --head \
           -A "Mozilla/5.0 (verify-page-urls)" "$clean" 2>/dev/null || echo "000")

  if [[ "$STATUS" =~ ^[23] ]]; then
    printf "  \033[32m✓\033[0m %s  %s\n" "$STATUS" "$clean"
    PASS=$((PASS+1))
  elif [[ "$ctx" == "social-meta" ]]; then
    printf "  \033[33m⚠\033[0m %s  %s  (og:image/twitter:image — social cards will lack thumbnail)\n" "$STATUS" "$clean"
    WARN=$((WARN+1))
  else
    printf "  \033[31m✗\033[0m %s  %s\n" "$STATUS" "$clean"
    FAIL=$((FAIL+1))
    FAILED_URLS+=("$clean")
  fi
done <<< "$ALL_URLS"

echo ""
echo "Total: $((PASS+FAIL+WARN+SKIP)) URLs — $PASS PASS, $FAIL FAIL, $WARN WARN, $SKIP SKIP"

# For GitHub repo URLs that failed, probe common naming variants
if [ "$FAIL" -gt 0 ]; then
  any_alt=0
  for url in "${FAILED_URLS[@]}"; do
    if [[ "$url" =~ ^https://github\.com/([^/]+)/([^/?#]+) ]]; then
      org="${BASH_REMATCH[1]}"
      repo="${BASH_REMATCH[2]}"
      declare -A seen
      seen["$repo"]=1
      variants=()

      # remove all hyphens
      if [[ "$repo" == *-* ]]; then
        v=$(printf '%s' "$repo" | tr -d -)
        [[ -z "${seen[$v]:-}" ]] && { variants+=("$v"); seen[$v]=1; }
      fi
      # trailing 'o' toggle
      if [[ "$repo" == *o ]]; then
        v="${repo%o}"
        [[ -z "${seen[$v]:-}" ]] && { variants+=("$v"); seen[$v]=1; }
      else
        v="${repo}o"
        [[ -z "${seen[$v]:-}" ]] && { variants+=("$v"); seen[$v]=1; }
      fi
      # underscore <-> hyphen
      if [[ "$repo" == *_* ]]; then
        v=$(printf '%s' "$repo" | tr _ -)
        [[ -z "${seen[$v]:-}" ]] && { variants+=("$v"); seen[$v]=1; }
      fi
      if [[ "$repo" == *-* ]]; then
        v=$(printf '%s' "$repo" | tr - _)
        [[ -z "${seen[$v]:-}" ]] && { variants+=("$v"); seen[$v]=1; }
      fi
      # insert hyphen before trailing letter group (heuristic: last 2-4 char block)
      for n in 2 3 4; do
        if [[ ${#repo} -gt $((n+1)) ]]; then
          prefix="${repo:0:$((${#repo}-n))}"
          suffix="${repo: -$n}"
          [[ "$prefix" == *-* ]] || {
            v="$prefix-$suffix"
            [[ -z "${seen[$v]:-}" ]] && { variants+=("$v"); seen[$v]=1; }
          }
        fi
      done

      [ ${#variants[@]} -gt 0 ] || continue
      first_hit=1
      for v in "${variants[@]}"; do
        cand="https://github.com/$org/$v"
        s=$(curl -sSo /dev/null -w "%{http_code}" -m 5 -L --head \
            -A "Mozilla/5.0 (verify-page-urls)" "$cand" 2>/dev/null || echo "000")
        if [[ "$s" =~ ^[23] ]]; then
          if [ "$first_hit" -eq 1 ]; then
            echo ""
            printf "  \033[33m⚠ Failed:\033[0m %s\n" "$url"
            first_hit=0
            any_alt=1
          fi
          printf "    \033[32m✓ ALTERNATIVE:\033[0m %s  (%s)\n" "$cand" "$s"
        fi
      done
      unset seen
    fi
  done
  [ "$any_alt" -eq 0 ] || echo ""
fi

[ "$FAIL" -eq 0 ] && exit 0 || exit 1
