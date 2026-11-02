FROM node:22-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/extra_ca; fi; \
    npm ci
COPY . .
RUN npm run build

# Static files served by nginx, which also reverse-proxies /api to the backend
# so the browser sees a single origin (simple CORS, SameSite cookies work).
FROM nginx:1.27-alpine
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
ENV API_UPSTREAM=http://api:8000
# Only substitute our variable, so nginx's own variables like $host are left untouched.
ENV NGINX_ENVSUBST_FILTER=API_UPSTREAM
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 8080
