#!/bin/bash
# One-off: fetch Elite Prospects player pages from Ondrej's Mac (home internet), politely. See docs/ep-mac-fetch.md.
# UNTESTED against Cloudflare: EP blocks this server since 25.10.2026. If a page is blocked, the script stops
# at once; never work around a block (no other tools, no proxies).
# Usage: bash ep_mac_fetch.sh ep-urls.txt ep-pages
# ep-urls.txt: lines "<athlete_id> <url>" from `klugscheisser ep urls --out`. Re-running continues where it stopped.
set -u
list="${1:?usage: bash ep_mac_fetch.sh ep-urls.txt ep-pages}"
out="${2:-ep-pages}"
max="${3:-300}" # daily cap, as on the server
mkdir -p "$out"
ua="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
n=0
while read -r aid url; do
  [ -z "${aid:-}" ] && continue
  case "$url" in https://www.eliteprospects.com/player/*) ;; *) continue ;; esac # player pages only (robots.txt)
  f="$out/$aid.html"
  [ -s "$f" ] && continue
  if [ "$n" -ge "$max" ]; then
    echo "Cap of $max pages reached. Run again tomorrow."
    exit 0
  fi
  code=$(curl -sS -L --max-time 60 -A "$ua" -H "Accept-Language: en" -o "$f.part" -w "%{http_code}" "$url")
  if [ "$code" != "200" ] || grep -qiE "captcha|challenge-platform|cf-chl|just a moment" "$f.part"; then
    rm -f "$f.part"
    echo "STOP at $aid: HTTP $code or a bot check. $n pages saved in $out. Do not retry today."
    exit 1
  fi
  mv "$f.part" "$f"
  n=$((n + 1))
  echo "$n  $aid  ok"
  sleep $((6 + RANDOM % 4))
done <"$list"
echo "Done: $n new pages in $out"
