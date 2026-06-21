#!/usr/bin/env bash
# ANGEL-09b: expose the direct Alexa custom-endpoint /guardian/alexa/skill at
# angel.darceesellers.com (in addition to the existing token route). Idempotent.
# Run with: sudo bash ~/projects/guardian/deploy/add-angel-skill-route.sh
set -euo pipefail

CONF=/etc/nginx/sites-available/angel-darceesellers
[ -f "$CONF" ] || { echo "ERROR: $CONF not found"; exit 1; }

if grep -q "location = /guardian/alexa/skill" "$CONF"; then
  echo "Route already present — nothing to change."
else
  cp "$CONF" "${CONF}.bak.$(date +%s)"
  python3 - "$CONF" <<'PY'
import sys
path = sys.argv[1]
src = open(path).read()
block = (
    "    location = /guardian/alexa/skill {\n"
    "        proxy_pass http://127.0.0.1:8101/guardian/alexa/skill;\n"
    "        proxy_set_header Host $host;\n"
    "        proxy_set_header X-Real-IP $remote_addr;\n"
    "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
    "        proxy_set_header X-Forwarded-Proto $scheme;\n"
    "        proxy_read_timeout 15s;\n"
    "    }\n"
)
anchor = "    location / { return 404; }"
assert anchor in src, "could not find the 'location / { return 404; }' anchor"
src = src.replace(anchor, block + anchor, 1)
open(path, "w").write(src)
print("Inserted /guardian/alexa/skill location block.")
PY
fi

echo "Testing nginx config..."
nginx -t
echo "Reloading nginx..."
systemctl reload nginx
echo "DONE. angel.darceesellers.com now proxies /guardian/alexa/skill -> 127.0.0.1:8101"
