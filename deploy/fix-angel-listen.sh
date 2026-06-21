#!/usr/bin/env bash
# ANGEL-09 fix: pin angel's SSL listen to the server IP (not generic :443) so it joins
# the same 31.220.96.150:443 socket group as the other vhosts and matches by server_name.
set -euo pipefail
F="/etc/nginx/sites-available/angel-darceesellers"
sed -i 's/^\([[:space:]]*\)listen 443 ssl;/\1listen 31.220.96.150:443 ssl;/' "$F"
nginx -t
systemctl reload nginx
echo "FIXED — current listen lines:"
grep -n "listen" "$F"
