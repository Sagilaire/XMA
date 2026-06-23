# --- Build stage -------------------------------------------------------------------
FROM node:18-alpine AS build

ARG VITE_API_BASE_URL=http://localhost:4015
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}

WORKDIR /app

# Install deps separately to take advantage of Docker layer caching.
COPY package.json ./
RUN npm install --no-audit --no-fund

COPY . .
RUN npm run build

# --- Production stage --------------------------------------------------------------
FROM nginx:alpine AS runtime

# Use a custom static-site config that listens on 3015 and forwards stray
# /api calls to the backend service when running behind compose.
COPY default.conf /etc/nginx/conf.d/default.conf

# Static build artefacts ship from the build stage.
COPY --from=build /app/dist /usr/share/nginx/html

# Friendly healthcheck so docker compose does not flag it as healthy too early.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD wget --quiet --tries=1 --spider http://127.0.0.1:3015/ || exit 1

EXPOSE 3015

CMD ["nginx", "-g", "daemon off;"]
