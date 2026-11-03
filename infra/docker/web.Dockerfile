# پنل وب: ساخت با Node و سرو با Caddy (HTTPS خودکار و پروکسی معکوس)
FROM node:22-alpine AS build
ARG NPM_REGISTRY=https://registry.npmjs.org
WORKDIR /web
RUN npm config set registry "$NPM_REGISTRY"
COPY apps/web/package.json apps/web/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund
COPY apps/web ./
RUN npm run build

FROM caddy:2.8-alpine
COPY infra/caddy/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /web/dist /srv
