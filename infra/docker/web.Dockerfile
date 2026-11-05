# پنل وب: ساخت با Node و سرو با Caddy (HTTPS خودکار و پروکسی معکوس)
FROM node:22-alpine AS build
ARG NPM_REGISTRY=https://registry.npmjs.org
WORKDIR /web
ARG NODE_HEAP_MB=1536
ENV NODE_OPTIONS="--max-old-space-size=${NODE_HEAP_MB}"
RUN npm config set registry "$NPM_REGISTRY"
COPY apps/web/package.json apps/web/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund
COPY apps/web ./
RUN npm run build

FROM caddy:2.8-alpine
COPY infra/caddy/Caddyfile /etc/caddy/Caddyfile
COPY infra/caddy/entrypoint.sh /usr/local/bin/xagent-web-entrypoint
RUN chmod +x /usr/local/bin/xagent-web-entrypoint
COPY --from=build /web/dist /srv
ENTRYPOINT ["/usr/local/bin/xagent-web-entrypoint"]
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
