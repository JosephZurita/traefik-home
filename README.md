# Traefik Home

![preview](/doc/preview.jpg)

Traefik Home creates a homepage from Traefik's **effective HTTP routers**. Each router is one card. It reads Traefik's runtime API rather than Docker labels or the Docker socket, so it supports Docker `defaultRule`, implicit/default entrypoints, file-provider routers, and multiple applications routed through one container (including Gluetun-style setups).

## How it works

The small Python service polls `/api/http/routers` and `/api/entrypoints`. It extracts the first effective `Host()` and practical `Path()`/`PathPrefix()` from each rule, then fetches that URL server-side to discover its HTML title and favicon. Icon links are resolved relative to the final page URL; `/favicon.ico` is tried next. Results and failures are cached on disk so clients never trigger application metadata requests and page loads do not repeatedly fetch icons.

No Docker socket or Docker API access is used. Router cards are never correlated or deduplicated by container, service, or IP.

## Deployment

Traefik's API must be reachable by this container. It need not be publicly exposed; enabling the dashboard API and routing `api@internal` on a private Docker network is recommended. For a simple trusted internal network, Traefik's `--api.insecure=true` exposes it on port 8080.

```yaml
services:
  traefik:
    image: traefik:v3.5
    command:
      - --api.insecure=true
      - --providers.docker=true
      - --providers.docker.exposedbydefault=false
      - --providers.docker.defaultRule=Host(`{{ normalize .Name }}.example.com`)
      - --entrypoints.web.address=:80
      - --entrypoints.websecure.address=:443
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro # Traefik needs this; Home does not

  traefik-home:
    image: ghcr.io/santimar/traefik-home:latest
    environment:
      TRAEFIK_URL: http://traefik:8080
    volumes:
      - traefik-home-cache:/data
      - ./traefik-home.toml:/config/traefik-home.toml:ro
    labels:
      traefik.enable: "true"
      traefik.http.routers.traefik-home.rule: Host(`home.example.com`)
      traefik.http.services.traefik-home.loadbalancer.server.port: "80"

volumes:
  traefik-home-cache:
```

If the API is exposed through an authenticated internal router instead, set `TRAEFIK_URL` to that URL. The current release supports an unauthenticated API endpoint; keep it private.

## Configuration

The optional `/config/traefik-home.toml` uses exact effective router names (normally including `@docker`) and/or hostnames. Router matches take precedence over hostname matches.

```toml
[ui]
show_status = true
open_in_new_tab = false

[overrides.router."admin@docker"]
hide = true

[overrides.router."qbittorrent@docker"]
title = "Downloads"
icon = "https://static.example.com/qbit.svg"
group = "Media"

[overrides.hostname."special.example.com"]
title = "Special app"
```

This deliberately avoids encoding homepage metadata in fake Traefik middleware configuration.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `TRAEFIK_URL` | `http://traefik:8080` | Traefik runtime API base URL |
| `CONFIG_FILE` | `/config/traefik-home.toml` | Override configuration |
| `CACHE_DIR` | `/data/icons` | Persistent favicon/metadata cache |
| `REFRESH_SECONDS` | `60` | Router refresh interval |
| `METADATA_REFRESH_SECONDS` | `86400` | Page title/favicon cache lifetime |
| `FETCH_TIMEOUT_SECONDS` | `5` | Traefik/application request timeout |
| `HTTP_ENTRYPOINTS` | `web` | Comma-separated scheme overrides |
| `HTTPS_ENTRYPOINTS` | `websecure` | Comma-separated scheme overrides |
| `DEFAULT_ENTRYPOINTS` | auto | Fallback when a runtime router omits entrypoints |
| `PORT` | `80` | Listen port |

Entrypoints marked `asDefault` by Traefik are preferred for routers that omit them. `DEFAULT_ENTRYPOINTS` covers older API versions/configurations that do not expose that flag. TLS routers always use HTTPS; otherwise explicit mappings and entrypoint addresses determine the scheme.

## Fallbacks and limitations

- Rules without a `Host()` cannot produce a public link and are skipped.
- For compound rules, the first host and first path matcher are used.
- Explicit title/icon overrides always win. Otherwise the HTML title and discovered icon are used. Failed icon discovery renders the existing colored initial tile.
- Persist `/data` to retain icons across restarts. Failed discovery is cached too and retried after `METADATA_REFRESH_SECONDS`.

## Tests

```sh
python -m unittest discover -s test -p 'test_*.py' -v
```

The suite covers labelled and `defaultRule` runtime routers, inherited entrypoints, multiple Gluetun-style routers, unrelated names for multiple application instances, favicon discovery and fallback behavior, generic icons, and filtering/overrides.
