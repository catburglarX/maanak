# Static frontend served by nginx, listening on 8080 as a non-root user.
FROM nginx:1.27-alpine

# Replace the default server config with ours (listens on 8080, proxies /api).
RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/nginx.conf

# Copy the site.
COPY . /usr/share/nginx/html
# Remove build-time artefacts that should never be served.
RUN rm -f /usr/share/nginx/html/Dockerfile \
          /usr/share/nginx/html/nginx.conf \
          /usr/share/nginx/html/_openapi.json

# Create writable temp dirs and hand ownership to the unprivileged nginx user
# that ships with the image (uid 101). The default prefix is root-owned, so the
# pid and temp paths are relocated to /tmp in nginx.conf.
RUN mkdir -p /tmp/client_temp /tmp/proxy_temp /tmp/fastcgi_temp \
             /tmp/uwsgi_temp /tmp/scgi_temp \
    && chown -R nginx:nginx /tmp/client_temp /tmp/proxy_temp /tmp/fastcgi_temp \
             /tmp/uwsgi_temp /tmp/scgi_temp /usr/share/nginx/html

USER nginx

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=5s --retries=10 \
  CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1

CMD ["nginx", "-g", "daemon off;"]
