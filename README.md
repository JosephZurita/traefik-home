# Traefik Home

![preview](/doc/preview.jpg)

Traefik Home creates a homepage from Traefik's **effective HTTP routers**. Each router is one card. Discovery uses Traefik's runtime API rather than Docker labels, the Docker API, container IPs, or the Docker socket. This supports Docker `defaultRule`, inherited entrypoints, file-provider routers, and multiple applications routed through one Gluetun-style container.

## How it works

The Python service polls `/api/http/routers` and `/api/entrypoints`. Internal and external entrypoint lists form an allowlist; routers on other entrypoints are omitted. Links always use HTTPS.

If an internal and external route share a hostname, their cards show an **External** badge. Comparison is case-insensitive and ignores the port. Cards remain router-based and are never deduplicated by hostname, service, container, or IP.

For every card, the service fetches page metadata server-side and tries to identify the hosted application from:

1. an explicit application override;
2. web-app manifest names and HTML application metadata;
3. the HTML title;
4. hostname tokens;
5. the router name as a low-confidence final hint.

Generic or ambiguous matches remain `unknown`. Detection uses a bundled, runtime-offline catalog of 3,100+ application names and aliases from [Homarr Dashboard Icons](https://github.com/homarr-labs/dashboard-icons), pinned to a reproducible commit. The image ships one high-quality local icon per catalog application—SVG where practical and 512px WebP otherwise. The catalog is Apache-2.0 licensed; product names and trademarks remain the property of their owners.

Icon selection follows this order:

1. explicit `icon` override;
2. high-resolution site icon, including SVG, web-app manifest, large declared icon, or Apple touch icon;
3. detected application's offline catalog icon;
4. ordinary page icon;
5. `/favicon.ico`;
6. generic initials.

Remote metadata and icons, including failed discovery, are cached under `/data/icons`. Catalog lookup never requires a network request.

## Deployment

Traefik's API must be reachable by this container. It need not be publicly exposed. For a simple trusted internal network, Traefik's `--api.insecure=true` exposes the API on port 8080.

```yaml
services:
  traefik:
    image: traefik:v3.5
    command:
      - --api.insecure=true
      - --providers.docker=true
      - --providers.docker.exposedbydefault=false
      - --providers.docker.defaultRule=Host(`{{ normalize .Name }}.example.com`)
      - --entrypoints.web.address=:443
      - --entrypoints.public.address=:8443
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro # Traefik needs this; Home does not

  traefik-home:
    image: ghcr.io/josephzurita/traefik-home:latest
    environment:
      TRAEFIK_URL: http://traefik:8080
      INTERNAL_ENTRYPOINTS: web
      EXTERNAL_ENTRYPOINTS: public
    volumes:
      - ./data:/data
    labels:
      traefik.enable: "true"
      traefik.http.routers.traefik-home.rule: Host(`home.example.com`)
      traefik.http.routers.traefik-home.entrypoints: web
      traefik.http.routers.traefik-home.tls: "true"
      traefik.http.services.traefik-home.loadbalancer.server.port: "80"
```

The current release cannot send authentication headers to Traefik. `TRAEFIK_URL` must point to an API endpoint that accepts unauthenticated requests; keep it private and reachable only on a trusted network.

## Configuration

The optional user-authored configuration is `/data/traefik-home.toml`. Configuration is loaded at startup, so restart the container after changing it.

```toml
[entrypoints]
internal = ["web"]
external = ["public"]

[ui]
show_status = true
open_in_new_tab = false

[routers."admin@docker"]
entrypoint = "web"
hide = true

[routers."photos@docker"]
entrypoint = "web"
application = "immich"
title = "Family photos"
icon = "catalog:immich"
group = "Media"
```

Router keys use exact effective names, normally including the provider suffix such as `@docker`. Supported router fields are:

| Field | Purpose |
|---|---|
| `entrypoint` | Preferred entrypoint; honored only when live and assigned to that router |
| `application` | Catalog slug or unique application name override |
| `title` | Explicit card title |
| `icon` | URL/path or `catalog:<application>` |
| `group` | Card group heading |
| `hide` | Exclude the router when `true` |

Hostname-wide defaults can be placed under `[hosts."name.example.com"]`; an exact `[routers."name@provider"]` section takes precedence. Host matching ignores the port.

If a configured entrypoint is invalid or removed, Home safely falls back to the router's live internal entrypoint, then its live external entrypoint. It never invents an entrypoint from configuration.

### Generated router inventory

After each successful refresh, `/data/routers.toml` is atomically replaced with an inventory of every active card. It is generated state and should not be edited. Every router always records its selected entrypoint:

```toml
[routers."photos@docker"]
hostname = "photos.example.com"
url = "https://photos.example.com"
entrypoint = "web"
exposure = "internal"
externally_available = false
status = "enabled"
title = "Immich"
application = "immich"
detection_confidence = 0.99
detection_source = "manifest-name"
icon = "catalog:immich"
```

Use this inventory to copy desired values into the user-authored `traefik-home.toml`. Keeping generated and manual files separate prevents refreshes from destroying comments or overwriting intentional overrides.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `TRAEFIK_URL` | `http://traefik:8080` | Traefik runtime API base URL |
| `CONFIG_FILE` | `/data/traefik-home.toml` | User configuration |
| `ROUTERS_FILE` | `/data/routers.toml` | Generated router inventory |
| `CACHE_DIR` | `/data/icons` | Persistent metadata/favicon cache |
| `REFRESH_SECONDS` | `60` | Router refresh interval |
| `METADATA_REFRESH_SECONDS` | `86400` | Page metadata cache lifetime |
| `FETCH_TIMEOUT_SECONDS` | `5` | Traefik/application request timeout |
| `INTERNAL_ENTRYPOINTS` | `web` | Comma-separated internal entrypoint allowlist |
| `EXTERNAL_ENTRYPOINTS` | `websecure` | Comma-separated external entrypoint allowlist |
| `DEFAULT_ENTRYPOINTS` | auto | Fallback for runtime routers that omit entrypoints |
| `PORT` | `80` | Listen port |

`INTERNAL_ENTRYPOINTS` and `EXTERNAL_ENTRYPOINTS` override their TOML lists when set. Their union is the discovery allowlist. When a router uses both types, its card prefers the internal entrypoint and displays the external badge.

Entrypoints marked `asDefault` by Traefik are preferred for routers that omit them. `DEFAULT_ENTRYPOINTS` covers API/configurations that do not expose that flag. All defaults and router entrypoints are intersected with both the exposure allowlist and live `/api/entrypoints`, so removed or unlisted entrypoints cannot appear.

## Catalog maintenance

The vendored catalog records its exact upstream commit and license under `app/catalog`. To deliberately refresh it, create a sparse checkout of that pinned/new Dashboard Icons revision containing `metadata.json`, `LICENSE`, `svg`, and `webp`, then run:

```sh
python scripts/vendor_dashboard_icons.py /path/to/dashboard-icons
```

Runtime startup and refresh never download catalog data.

## Limitations

- Rules without a `Host()` cannot produce a public link and are skipped.
- For compound rules, the first host and first path matcher are used.
- Application detection is intentionally conservative and may report `unknown`.
- Metadata discovery requires the application HTTPS URL to be reachable and its certificate to be trusted by the container.
- Persist `/data` to retain cached metadata, icons, configuration, and generated inventory.

## Tests

```sh
python -m unittest discover -s test -p 'test_*.py' -v
```

The mocked runtime-API suite covers labelled and `defaultRule` routers, inherited entrypoints, internal/external filtering, port-independent external indicators, application detection and ambiguity, high-resolution icon ranking, offline catalog fallback, explicit overrides, generated inventories, Gluetun-style routers, favicon fallback, and generic icons.

## Publishing containers

The GitHub Actions workflow builds `linux/amd64`, `linux/arm64`, and `linux/arm/v7` images in GHCR. Run the **Docker** workflow manually with a `release_tag`; it publishes that tag and updates `latest`. Manual dispatch is the reliable release path for forks where inherited tag events do not start Actions runs.

```sh
docker pull ghcr.io/josephzurita/traefik-home:latest
```
