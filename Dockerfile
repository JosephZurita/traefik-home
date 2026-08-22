FROM python:3.13-alpine
WORKDIR /app
COPY app /app
COPY static /app/static
ENV TRAEFIK_URL=http://traefik:8080 CONFIG_FILE=/data/traefik-home.toml ROUTERS_FILE=/data/routers.toml CACHE_DIR=/data/icons PORT=80
VOLUME ["/data"]
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD wget -q -O /dev/null http://127.0.0.1/health || exit 1
CMD ["python", "/app/server.py"]
