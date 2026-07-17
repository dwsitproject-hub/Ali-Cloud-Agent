# Cloud Agent Monitoring — standalone frontend (static UI + API proxy under nginx).
# Serves the dashboard/instances/login pages AND reverse-proxies /api and /auth to
# the backend, so the whole app is one origin. API_BASE (browser) and
# BACKEND_ORIGIN (nginx proxy) are baked in at container start.
FROM nginx:1.27-alpine

# Static assets.
COPY public/ /usr/share/nginx/html/

# Server config template (rendered by the entrypoint below).
COPY default.conf.template /etc/nginx/conf.d/default.conf.template

# Runtime renderer for config.js + default.conf (runs before nginx starts).
COPY 40-config-js.sh /docker-entrypoint.d/40-config-js.sh
RUN chmod +x /docker-entrypoint.d/40-config-js.sh

EXPOSE 80
