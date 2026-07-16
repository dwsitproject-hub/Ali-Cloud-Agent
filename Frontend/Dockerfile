# Cloud Agent Monitoring — standalone frontend (static site under nginx).
# Serves the dashboard/instances/login pages and calls the backend API
# cross-origin. API_BASE (build/run env) is baked into config.js at start.
FROM nginx:1.27-alpine

# Static assets.
COPY public/ /usr/share/nginx/html/

# Server config.
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Runtime config.js renderer (runs before nginx starts).
COPY 40-config-js.sh /docker-entrypoint.d/40-config-js.sh
RUN chmod +x /docker-entrypoint.d/40-config-js.sh

EXPOSE 80
