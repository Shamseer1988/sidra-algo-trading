FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json ./
COPY apps/web/package.json ./apps/web/package.json
RUN npm install --workspace @intraday-sentinel/web

FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules

COPY package.json ./
COPY apps/web ./apps/web
RUN mkdir -p /app/apps/web/public
ARG NEXT_PUBLIC_API_BASE_URL=/api
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
# Next.js evaluates rewrites while producing the standalone server.  Supply the
# Docker network hostname at build time; localhost would mean the web container
# itself, not the API service.
ARG API_UPSTREAM=http://api:8000
ENV API_UPSTREAM=$API_UPSTREAM
RUN npm --workspace @intraday-sentinel/web run build

FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production PORT=3000 HOSTNAME="0.0.0.0"
COPY --from=builder /app/apps/web/.next/standalone ./
COPY --from=builder /app/apps/web/.next/static ./apps/web/.next/static
COPY --from=builder /app/apps/web/public ./apps/web/public
EXPOSE 3000
CMD ["node", "apps/web/server.js"]
