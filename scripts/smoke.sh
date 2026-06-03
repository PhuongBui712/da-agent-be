#!/usr/bin/env bash
# End-to-end smoke walkthrough.
#
# Verifies the wire format between FE and BE. Assumes:
#   - BE running at http://127.0.0.1:8765 (override via DA_AGENT_BASE_URL)
#   - tests/fixtures/sales.xlsx exists (run tests/fixtures/_gen_sales_xlsx.py first)
#
# Steps (each step is a shell-scriptable [S] check from the plan):
#   1.  GET /health -> {"ok": true}
#   2.  POST /sessions -> 201 SessionResponse
#   3.  POST /kb/files (multipart) -> 202 KbFileResponse with status PENDING|PROCESSING
#   4.  Poll GET /kb/files/<id> until status=READY (timeout 30s)
#   5.  GET /kb/files/<id>/manifest -> 200 with sheets containing Customers/Products/Sales
#   6.  POST /sessions/<sid>/attachments (multipart) -> 201 AttachmentResponse
#   7.  GET /sessions/<sid>/attachments -> list contains the new attachment
#   8.  POST /kb/files/import-sheet -> 400 on bogus URL (real endpoint, not stub)
#   9.  GET /kb/files/<id>/versions -> 200 (likely empty until agent writes)
#  10.  GET /outputs?session_id=<sid> -> 200 (empty initially)
#  11.  POST /sessions/<sid>/messages with kb_scope=[]   -> 400 (validation)
#  12.  POST /sessions/<sid>/messages with kb_scope=["bogus"] -> 400 (unknown id)
#  13.  DELETE /sessions/<sid>/attachments/<att_id> -> 204
#  14.  DELETE /kb/files/<id> -> 204
#  15.  DELETE /sessions/<sid> -> 204; attachments dir gone
#
# This script DOES NOT trigger a real model turn (that requires ANTHROPIC_API_KEY
# and incurs cost/latency). It only verifies HTTP wire shapes; the live walkthrough
# in the README handles the SSE end-to-end check.

set -euo pipefail

BASE="${DA_AGENT_BASE_URL:-http://127.0.0.1:8765}"
FIXTURE="$(cd "$(dirname "$0")/.." && pwd)/tests/fixtures/sales.xlsx"
ATTACH_FIXTURE="$(mktemp -t da-agent-attach.XXXXXX)"
echo "small attachment payload" > "$ATTACH_FIXTURE"
trap 'rm -f "$ATTACH_FIXTURE"' EXIT

green() { printf '\033[32m%s\033[0m\n' "$1"; }
red()   { printf '\033[31m%s\033[0m\n' "$1"; }
yellow(){ printf '\033[33m%s\033[0m\n' "$1"; }

step() { echo; yellow ">> $1"; }
ok()   { green   "   OK: $1"; }
fail() { red     "   FAIL: $1"; exit 1; }

require_jq() { command -v jq >/dev/null 2>&1 || { red "jq is required (apt install jq / brew install jq)"; exit 2; }; }
require_curl(){ command -v curl >/dev/null 2>&1 || { red "curl is required"; exit 2; }; }

require_jq
require_curl

[[ -f "$FIXTURE" ]] || fail "fixture missing: $FIXTURE (run uv run python tests/fixtures/_gen_sales_xlsx.py)"

# 1. Health
step "GET $BASE/health"
HEALTH=$(curl -fsS "$BASE/health")
[[ "$(echo "$HEALTH" | jq -r '.ok')" == "true" ]] || fail "health check did not return ok=true"
ok "health=$HEALTH"

# 2. Create session
step "POST $BASE/sessions"
SESSION=$(curl -fsS -H 'Content-Type: application/json' \
  -d '{"name":"smoke-walkthrough"}' "$BASE/sessions")
SID=$(echo "$SESSION" | jq -r '.id')
[[ "$SID" == sess_* ]] || fail "expected sess_ id, got: $SID"
ok "session=$SID"

# 3. Upload KB
step "POST $BASE/kb/files (multipart)"
KB_RESP=$(curl -fsS -F "file=@${FIXTURE}" "$BASE/kb/files")
KB_ID=$(echo "$KB_RESP" | jq -r '.id')
KB_STATUS=$(echo "$KB_RESP" | jq -r '.status')
[[ "$KB_ID" == kb_* ]] || fail "expected kb_ id, got: $KB_ID"
[[ "$KB_STATUS" == "PENDING" || "$KB_STATUS" == "PROCESSING" ]] || fail "unexpected status: $KB_STATUS"
ok "kb=$KB_ID status=$KB_STATUS"

# 4. Poll until READY
step "Poll GET $BASE/kb/files/$KB_ID until READY"
DEADLINE=$(($(date +%s) + 30))
while true; do
  CUR=$(curl -fsS "$BASE/kb/files/$KB_ID")
  CUR_STATUS=$(echo "$CUR" | jq -r '.status')
  if [[ "$CUR_STATUS" == "READY" ]]; then ok "READY"; break; fi
  if [[ "$CUR_STATUS" == "FAILED" ]]; then fail "preprocessing FAILED: $(echo "$CUR" | jq -r '.error')"; fi
  if (( $(date +%s) > DEADLINE )); then fail "timeout waiting for READY (last=$CUR_STATUS)"; fi
  sleep 1
done

# 5. Manifest
step "GET $BASE/kb/files/$KB_ID/manifest"
MAN=$(curl -fsS "$BASE/kb/files/$KB_ID/manifest")
echo "$MAN" | jq -e '.sheets | map(.name) | (index("Customers") and index("Products") and index("Sales"))' >/dev/null \
  || fail "manifest missing expected sheets"
ok "manifest has Customers/Products/Sales"

# 6. Upload attachment
step "POST $BASE/sessions/$SID/attachments"
ATT_RESP=$(curl -fsS -F "file=@${ATTACH_FIXTURE};filename=note.txt" "$BASE/sessions/$SID/attachments")
ATT_ID=$(echo "$ATT_RESP" | jq -r '.attachment_id')
[[ "$ATT_ID" == att_* ]] || fail "expected att_ id, got: $ATT_ID"
ok "attachment=$ATT_ID"

# 7. List attachments
step "GET $BASE/sessions/$SID/attachments"
ATTLIST=$(curl -fsS "$BASE/sessions/$SID/attachments")
echo "$ATTLIST" | jq -e --arg id "$ATT_ID" '.attachments | map(.attachment_id) | index($id)' >/dev/null \
  || fail "newly-uploaded attachment missing from list"
ok "attachment listed"

# 8. Sheets import — real endpoint, validates URL shape (bogus host → 400)
step "POST $BASE/kb/files/import-sheet (bogus URL, expect 400)"
SHEETS_CODE=$(curl -s -o /dev/null -w '%{http_code}' \
  -H 'Content-Type: application/json' -d '{"name":"x","url":"https://docs.google.com/x"}' \
  "$BASE/kb/files/import-sheet")
[[ "$SHEETS_CODE" == "400" ]] || fail "expected 400 from /kb/files/import-sheet on bogus URL, got $SHEETS_CODE"
ok "400 (invalid URL — real endpoint live)"

# 9. KB versions
step "GET $BASE/kb/files/$KB_ID/versions"
VERS=$(curl -fsS "$BASE/kb/files/$KB_ID/versions")
echo "$VERS" | jq -e '.versions | type == "array"' >/dev/null || fail "versions response shape wrong"
ok "versions=$(echo "$VERS" | jq -r '.versions | length')"

# 10. Outputs
step "GET $BASE/outputs?session_id=$SID"
OUTS=$(curl -fsS "$BASE/outputs?session_id=$SID")
echo "$OUTS" | jq -e '.outputs | type == "array"' >/dev/null || fail "outputs response shape wrong"
ok "outputs=$(echo "$OUTS" | jq -r '.outputs | length')"

# 11. kb_scope=[] -> 200 (2026-06-02 semantics: empty == empty scope, not 400)
step "POST $BASE/sessions/$SID/messages kb_scope=[] (expect 200, drains)"
SCOPE_EMPTY=$(curl -s -o /tmp/_scope_empty.txt -w '%{http_code}' \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"hi","kb_scope":[]}' \
  "$BASE/sessions/$SID/messages")
[[ "$SCOPE_EMPTY" == "200" ]] || fail "expected 200, got $SCOPE_EMPTY"
grep -q "no KB files are in scope\|user.prompt" /tmp/_scope_empty.txt \
  || fail "expected empty <scope> markers in response body"
ok "200 + empty scope rendered"

# 12. unknown kb_id -> 400
step "POST $BASE/sessions/$SID/messages kb_scope=[bogus] (expect 400)"
SCOPE_BAD=$(curl -s -o /tmp/_scope_bad.json -w '%{http_code}' \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"hi","kb_scope":["kb_does_not_exist"]}' \
  "$BASE/sessions/$SID/messages")
[[ "$SCOPE_BAD" == "400" ]] || fail "expected 400, got $SCOPE_BAD"
ERR_MSG=$(jq -r '.detail.error // .error // ""' </tmp/_scope_bad.json)
[[ "$ERR_MSG" == *"unknown kb_id"* ]] || fail "wrong error body: $ERR_MSG"
ok "400 + unknown kb_id"

# 13. Delete attachment
step "DELETE $BASE/sessions/$SID/attachments/$ATT_ID"
ATT_DEL=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$BASE/sessions/$SID/attachments/$ATT_ID")
[[ "$ATT_DEL" == "204" ]] || fail "expected 204, got $ATT_DEL"
ok "attachment deleted"

# 14. Delete KB
step "DELETE $BASE/kb/files/$KB_ID"
KB_DEL=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$BASE/kb/files/$KB_ID")
[[ "$KB_DEL" == "204" ]] || fail "expected 204, got $KB_DEL"
ok "kb deleted"

# 15. Delete session
step "DELETE $BASE/sessions/$SID"
SES_DEL=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$BASE/sessions/$SID")
[[ "$SES_DEL" == "204" ]] || fail "expected 204, got $SES_DEL"
ok "session deleted"

echo
green "all 15 wire-format checks passed."
green "next: open http://localhost:3000 (npm run dev) and walk the visual checks in the plan."
