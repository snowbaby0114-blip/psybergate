# syntax=docker/dockerfile:1

# Build the SPA. `npm ci` installs exactly what package-lock.json pins, so the build is reproducible.
# Unit tests can be run in this stage's image: docker build --target build ./frontend, then `npm test`.
FROM node:24-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

# Serve it. The nginx image's own default site is replaced by ours.
FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist/frontend/browser /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1
