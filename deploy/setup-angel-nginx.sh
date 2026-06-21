#!/usr/bin/env bash
# ANGEL-09: stand up the HTTPS endpoint for angel.darceesellers.com (Alexa route only).
# Run with: sudo bash ~/projects/guardian/deploy/setup-angel-nginx.sh
set -euo pipefail

SRC="/home/darcee/projects/guardian/deploy/nginx-angel.conf"
AVAIL="/etc/nginx/sites-available/angel-darceesellers"
ENABLED="/etc/nginx/sites-enabled/angel-darceesellers"

echo "==> Installing server block"
cp "$SRC" "$AVAIL"
ln -sf "$AVAIL" "$ENABLED"

echo "==> Testing + reloading nginx (HTTP)"
nginx -t
systemctl reload nginx

echo "==> Obtaining/installing cert (certbot --nginx)"
certbot --nginx -d angel.darceesellers.com --redirect -n --agree-tos -m darceejsellers@gmail.com

echo "==> Final nginx test + reload"
nginx -t
systemctl reload nginx

echo "DONE: https://angel.darceesellers.com/guardian/alexa/wellness is live (token-protected)."
