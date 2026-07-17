#!/bin/sh
# Rendered at container start (the official nginx image runs every *.sh in
# /docker-entrypoint.d before starting nginx):
#   * config.js       <- API_BASE        (where the browser calls the API)
#   * default.conf    <- BACKEND_ORIGIN  (where nginx proxies /api and /auth)
# Single-origin setups set API_BASE to this same origin (or leave it "" for a
# relative base); BACKEND_ORIGIN points at the backend service.
set -e
: "${API_BASE:=http://localhost:5000}"
: "${BACKEND_ORIGIN:=http://127.0.0.1:5000}"
export API_BASE BACKEND_ORIGIN

envsubst '${API_BASE}' \
  < /usr/share/nginx/html/config.js.template \
  > /usr/share/nginx/html/config.js

envsubst '${BACKEND_ORIGIN}' \
  < /etc/nginx/conf.d/default.conf.template \
  > /etc/nginx/conf.d/default.conf

echo "FE config: API_BASE=${API_BASE} | BACKEND_ORIGIN=${BACKEND_ORIGIN}"
