# The ui image: nginx serving the static chat page. No build step, so one stage.
# Build context: ui/   ->   docker build -t rahbar-ui:dev ui
#
# nginx-unprivileged already runs as uid 101; nginx.conf listens on 8080 and writes
# only under /tmp, so the pod can use readOnlyRootFilesystem with an emptyDir at /tmp.
FROM nginxinc/nginx-unprivileged:1.29.8-alpine
# The base image lags behind Alpine security fixes (Trivy found HIGH and CRITICAL ones in
# curl, brotli and c-ares), so upgrade its packages as root, then drop back to uid 101.
USER root
RUN apk upgrade --no-cache
COPY nginx.conf /etc/nginx/nginx.conf
COPY index.html /usr/share/nginx/html/index.html
USER 101
EXPOSE 8080
# Run nginx directly. The base image's entrypoint scripts rewrite files under /etc/nginx,
# which fails on a read-only root filesystem, and our nginx.conf does not need them.
ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]
