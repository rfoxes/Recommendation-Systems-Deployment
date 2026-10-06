#!/usr/bin/env bash
# Walk through the API like a reviewer: the README flow, GeoIP/OS targeting, brand safety, clicks, then a full
# campaign lifecycle (create -> ad set -> Temporal writes LLM copy -> serve -> delete).
#
#   scripts/demo.sh <base-url> [readme|lifecycle|all]      (default: all)
#   PAUSE=1 scripts/demo.sh ...                            wait for Enter between steps (for narrating)
#   CLICK_API_KEY=... scripts/demo.sh ...                  the deployment's click key (default: dev-click-key)
set -euo pipefail

BASE="${1:?usage: scripts/demo.sh <base-url> [readme|lifecycle|all]}"; BASE="${BASE%/}"
PART="${2:-all}"
CLICK_KEY="${CLICK_API_KEY:-dev-click-key}"
IPHONE='Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)'
ANDROID='Mozilla/5.0 (Linux; Android 15; Pixel 9)'
README_CONTEXT='{"searchTerm": "space adventure", "tags": ["sci-fi", "rpg"], "category": "roleplay", "title": "Galaxy Companion", "nsfw": false}'
RUN="$(date +%s)"

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
step() { bold "── $*"; if [ "${PAUSE:-0}" = 1 ]; then read -r -p "   (Enter) " _; fi; }
show() { printf '\033[2m$ %s\033[0m\n' "$*" >&2; }
json() { python3 -c 'import json, sys; print(json.dumps(json.load(sys.stdin), indent=2, ensure_ascii=False))'; }
field() { python3 -c "import json, sys; print(json.load(sys.stdin)$1)"; }

# POST helper: prints the request, returns "<status>\n<body>"
post() {  # post <path> <json-body> [curl args...]
  local path="$1" body="$2"; shift 2
  show "curl -X POST $BASE$path $* -d '$body'"
  curl -s -w '\n%{http_code}' -X POST "$BASE$path" -H 'Content-Type: application/json' "$@" -d "$body"
}
status_of() { tail -n 1; }
body_of() { sed '$d'; }

session() {  # session <ip> <ppid> -> prints "<status> <session_id>"
  local out; out="$(post /session/create "{\"ppid\": \"$2\"}" -H "X-Forwarded-For: $1")"
  echo "$(printf '%s\n' "$out" | status_of) $(printf '%s\n' "$out" | body_of | field '["session_id"]')"
}

serve() {  # serve <ip> <ua> <session_id> [context-json] -> prints a one-line summary
  local out code; out="$(post /load/native "{\"position\": 3, \"session_id\": \"$3\", \"context\": ${4:-$README_CONTEXT}}" -H "X-Forwarded-For: $1" -A "$2")"
  code="$(printf '%s\n' "$out" | status_of)"
  if [ "$code" = 200 ]; then
    AD_JSON="$(printf '%s\n' "$out" | body_of)" python3 - <<'PY'
import json, os, re
d = json.loads(os.environ["AD_JSON"])
h = d["rendered_html"]


def get(name):
    match = re.search(r'const ' + name + r'\s*=\s*"(.*?)";', h)
    return json.loads('"' + match.group(1) + '"')


print(f'   200  {d["impression_id"]}  @{get("CAMPAIGN")} · {get("CHAR_NAME")}: "{get("CHAR_MESSAGE")}" [{get("CTA")}]')
print(f'        click-through: {get("TRACKING_URL")}  ({len(h):,} bytes of rendered HTML)')
PY
  else
    echo "   $code  $([ "$code" = 204 ] && echo 'no fill: no eligible campaign' || printf '%s\n' "$out" | body_of)"
  fi
  LAST_IMPRESSION="$(printf '%s\n' "$out" | body_of | python3 -c 'import json,sys; print(json.load(sys.stdin).get("impression_id",""))' 2>/dev/null || true)"
}

readme_flow() {
  step "Readiness: every dependency, the CTR model and the LLM"
  show "curl $BASE/ready?format=json"; curl -s "$BASE/ready?format=json" | json

  step "Sessions: resolve-or-create (README: only a new session after 30 s without ad serves)"
  read -r code1 sid <<<"$(session 214.78.0.1 "demo_user_$RUN")"; echo "   $code1  $sid   (new)"
  read -r code2 sid2 <<<"$(session 214.78.0.1 "demo_user_$RUN")"; echo "   $code2  $sid2   (same session while active)"

  step "The README's sample request: US test IP, iPhone"
  serve 214.78.0.1 "$IPHONE" "$sid"; README_IMPRESSION="$LAST_IMPRESSION"

  step "OS targeting: same user on Android (Galaxy Quest targets iOS only)"
  serve 214.78.0.1 "$ANDROID" "$(session 214.78.0.1 "demo_android_$RUN" | cut -d' ' -f2)"

  step "GeoIP targeting with every README test IP (Android)"
  for pair in "214.78.0.1 US" "2.125.160.217 GB" "89.160.20.113 SE" "175.16.199.1 CN" "202.196.224.1 PH" "67.43.156.1 BT"; do
    set -- $pair; printf '   %s (%s):' "$2" "$1"
    serve "$1" "$ANDROID" "$(session "$1" "geo_${2}_$RUN" | cut -d' ' -f2)" | head -n 1
  done

  step "Brand safety: an NSFW chat (campaigns accept sfw contexts by default)"
  serve 214.78.0.1 "$IPHONE" "$sid" '{"title": "Late Night Chat", "nsfw": true}'

  step "Errors: unknown session, invalid request"
  serve 214.78.0.1 "$IPHONE" sess_does_not_exist
  show "curl -X POST $BASE/load/native -d '{\"position\": -1}'"
  curl -s -w '   -> %{http_code}\n' -o /dev/null -X POST "$BASE/load/native" -H 'Content-Type: application/json' -d '{"position": -1}'

  step "Clicks (the ad's CTA calls this): wrong key, first click, repeat"
  for key in wrong-key "$CLICK_KEY" "$CLICK_KEY"; do
    show "curl -X POST $BASE/impressions/$README_IMPRESSION/click -H 'Authorization: Bearer ${key:0:6}…'"
    curl -s -o /dev/null -w '   -> %{http_code}\n' -X POST "$BASE/impressions/$README_IMPRESSION/click" -H "Authorization: Bearer $key"
  done
}

lifecycle_flow() {
  local body cid ad_set_id
  step "Create a campaign targeting Bhutan (README test IP 67.43.156.1), with an Idempotency-Key"
  body="{\"campaign_name\": \"Demo $RUN — Dragon Raid\", \"advertiser_company_id\": \"acmp_demo\", \"geo_targets\": [\"BT\"], \"os_targets\": [\"ios\", \"android\"], \"ios_store_url\": \"https://apps.apple.com/app/id1\", \"android_store_url\": \"https://play.google.com/store/apps/details?id=demo\", \"downloads_label\": \"1.2M\"}"
  out="$(post /campaigns "$body" -H "Idempotency-Key: demo-camp-$RUN")"; cid="$(printf '%s\n' "$out" | body_of | field '["campaign_id"]')"
  echo "   $(printf '%s\n' "$out" | status_of)  $cid   active=$(printf '%s\n' "$out" | body_of | field '["active"]')"
  out="$(post /campaigns "$body" -H "Idempotency-Key: demo-camp-$RUN")"
  echo "   retry with the same key -> $(printf '%s\n' "$out" | status_of)  $(printf '%s\n' "$out" | body_of | field '["campaign_id"]')  (no duplicate)"

  step "Create an ad set: 2 characters x 1 video x 2 CTAs x 1 prompt = 4 variants (one transaction)"
  body="{\"campaign_id\": \"$cid\", \"ad_set_name\": \"Dragon riders\", \"character_names\": [\"Aria\", \"Kael\"], \"video_urls\": [\"https://storage.googleapis.com/simula-public/assets/simula-campaigns/1781322499823-8.mp4\"], \"ctas\": [\"Ride Now\", \"Join the Raid\"], \"ai_prompts\": [\"Tell the user the dragon raid starts tonight and you saved them a spot.\"], \"fallback_copy\": [\"Fallback: the raid starts tonight!\"]}"
  out="$(post /adsets "$body" -H "Idempotency-Key: demo-adset-$RUN")"
  printf '%s\n' "$out" | body_of | python3 -c 'import json,sys; d=json.load(sys.stdin); print("   201 ", d["ad_set_id"]); [print("        ", v["variant_id"], v["character_name"], "/", v["cta"]) for v in d["variants"]]'

  step "Activate it (PATCH), and see it served from the cache-backed list"
  show "curl -X PATCH $BASE/campaigns/$cid -d '{\"active\": true}'"
  curl -s -X PATCH "$BASE/campaigns/$cid" -H 'Content-Type: application/json' -d '{"active": true}' | python3 -c 'import json,sys; d=json.load(sys.stdin); print("   active:", d["active"], "| version:", d["version"], "| ad sets:", d["native_ad_set_ids"])'
  show "curl $BASE/campaigns?active=true"
  curl -s "$BASE/campaigns?active=true" | python3 -c 'import json,sys; print("   active campaigns:", [c["campaign_id"] for c in json.load(sys.stdin)])'

  step "Temporal is writing LLM copy for the 4 new variants (watch GenerateAdCopy in the Temporal UI)"
  for i in $(seq 1 30); do
    src="$(curl -s -X POST "$BASE/demo/serve" -H 'Content-Type: application/json' -d "{\"ip\": \"67.43.156.1\", \"device\": \"ios\", \"ppid\": \"copy_check_${RUN}_$i\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("debug", {}).get("copy_source", "?"))' 2>/dev/null || echo '?')"
    [ "$src" = pool ] && { echo "   copy ready after ~$((i * 5)) s (served from the pre-generated pool)"; break; }
    sleep 5
  done

  step "Serve it in Bhutan: LLM-written line, not the fallback"
  serve 67.43.156.1 "$IPHONE" "$(session 67.43.156.1 "bt_user_$RUN" | cut -d' ' -f2)" '{"title": "Dragon Rider", "tags": ["fantasy"], "nsfw": false}'

  step "Delete the campaign (cascades to its ad set and variants in one transaction) and serve again"
  show "curl -X DELETE $BASE/campaigns/$cid"
  curl -s -o /dev/null -w '   -> %{http_code}\n' -X DELETE "$BASE/campaigns/$cid"
  serve 67.43.156.1 "$IPHONE" "$(session 67.43.156.1 "bt_user2_$RUN" | cut -d' ' -f2)"
}

case "$PART" in
  readme) readme_flow ;;
  lifecycle) lifecycle_flow ;;
  all) readme_flow; lifecycle_flow ;;
  *) echo "part must be readme, lifecycle or all" >&2; exit 2 ;;
esac
bold "Done."
