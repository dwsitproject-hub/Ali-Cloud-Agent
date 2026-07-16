#!/bin/sh
# Render config.js from the API_BASE env var at container start. The official
# nginx image runs every *.sh in /docker-entrypoint.d before starting nginx.
set -e
: "${API_BASE:=http://localhost:5000}"
export API_BASE
envsubst '${API_BASE}' \
  < /usr/share/nginx/html/config.js.template \
  > /usr/share/nginx/html/config.js
echo "config.js rendered with API_BASE=${API_BASE}"
