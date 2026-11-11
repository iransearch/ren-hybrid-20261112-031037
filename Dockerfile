# HAProxy LTS image, pinned to the verified multi-platform manifest.
FROM haproxy:3.2.26-alpine3.24@sha256:1c3568e520453cc8ba98c39501febcf450429c80e6cc27ecad779961d76e597c
USER root
RUN apk add --no-cache python3 ca-certificates \
    && addgroup -g 10001 ren \
    && adduser -D -H -u 10001 -G ren ren
WORKDIR /app
COPY --chown=ren:ren main.py cloudflare_origin_ca_roots.pem ./
USER ren
ENV PYTHONUNBUFFERED=1 PORT=10000 MAX_CONCURRENT=0
EXPOSE 10000
STOPSIGNAL SIGTERM
ENTRYPOINT ["python3", "/app/main.py"]
CMD []
