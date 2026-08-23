#!/usr/bin/env python3
"""Traefik runtime-API homepage with offline application detection."""
from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import os
import re
import threading
import time
import tomllib
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).parent
STATIC = next(path for path in (APP_DIR / "static", APP_DIR.parent / "static") if path.exists())
CATALOG_FILE = APP_DIR / "catalog" / "applications.json"
HOST_RE = re.compile(r"Host\s*\(\s*[`\"']([^`\"']+)", re.I)
PATH_RE = re.compile(r"Path(?:Prefix)?\s*\(\s*[`\"']([^`\"']+)", re.I)
SIZE_RE = re.compile(r"(\d+)x(\d+)", re.I)
GENERIC_NAMES = {
    "app", "application", "dashboard", "home", "index", "log in", "login",
    "media", "media server", "server", "service", "sign in", "tools", "web",
}
CACHE_VERSION = 3


def csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def normalize(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())


def hostname_without_port(value: str) -> str:
    try:
        return (urllib.parse.urlsplit("//" + value).hostname or value).lower().rstrip(".")
    except ValueError:
        return value.lower().rstrip(".")


def route_url_key(value: str) -> str:
    """Normalize only URL components that cannot distinguish HTTP routes."""
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
        authority = hostname if not port or port == 443 else f"{hostname}:{port}"
        return urllib.parse.urlunsplit((parsed.scheme.lower(), authority, parsed.path, parsed.query, ""))
    except ValueError:
        return value


def declared_icon_size(value: str) -> int:
    if "any" in value.lower():
        return 4096
    sizes = [max(int(width), int(height)) for width, height in SIZE_RE.findall(value)]
    return max(sizes, default=0)


@dataclass(frozen=True)
class IconCandidate:
    url: str
    source: str
    size: int = 0
    content_type: str = ""

    @property
    def is_svg(self) -> bool:
        path = urllib.parse.urlparse(self.url).path.lower()
        return self.content_type == "image/svg+xml" or path.endswith(".svg")

    @property
    def score(self) -> int:
        if self.is_svg:
            return 100_000
        if self.source == "manifest":
            return 80_000 + self.size
        if self.source == "apple-touch-icon":
            return 70_000 + self.size
        if self.size >= 96:
            return 60_000 + self.size
        return 1_000 + self.size

    @property
    def high_quality(self) -> bool:
        return self.score >= 60_000


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside_title = False
        self.title_parts: list[str] = []
        self.icons: list[dict] = []
        self.manifests: list[str] = []
        self.metadata: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        attrs = {key.lower(): value for key, value in attrs if value is not None}
        tag = tag.lower()
        if tag == "title":
            self.inside_title = True
        if tag == "meta" and attrs.get("content"):
            key = (attrs.get("name") or attrs.get("property") or "").lower()
            if key in {"application-name", "apple-mobile-web-app-title", "og:site_name", "generator"}:
                self.metadata.append((key, attrs["content"]))
        if tag != "link" or not attrs.get("href"):
            return
        rel = set(attrs.get("rel", "").lower().split())
        if "manifest" in rel:
            self.manifests.append(attrs["href"])
        if "icon" in rel or "apple-touch-icon" in rel or "mask-icon" in rel:
            source = "apple-touch-icon" if "apple-touch-icon" in rel else "icon"
            self.icons.append({
                "href": attrs["href"],
                "source": source,
                "size": declared_icon_size(attrs.get("sizes", "")),
                "type": attrs.get("type", "").lower(),
            })

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self.inside_title = False

    def handle_data(self, data):
        if self.inside_title:
            self.title_parts.append(data)

    @property
    def title(self) -> str:
        return " ".join("".join(self.title_parts).split())


@dataclass(frozen=True)
class Detection:
    slug: str = ""
    name: str = ""
    confidence: float = 0.0
    source: str = ""
    icon: str = ""


class ApplicationCatalog:
    def __init__(self, filename: Path = CATALOG_FILE):
        try:
            data = json.loads(filename.read_text("utf-8"))
        except (OSError, ValueError):
            data = {"source": {}, "applications": {}}
        self.source = data.get("source", {})
        self.applications = data.get("applications", {})
        aliases: dict[str, set[str]] = {}
        canonical: dict[str, set[str]] = {}
        for slug, item in self.applications.items():
            for value in (slug, item.get("name", "")):
                key = normalize(value)
                if key:
                    canonical.setdefault(key, set()).add(slug)
            for alias in {slug, item.get("name", ""), *item.get("aliases", [])}:
                key = normalize(alias)
                if key:
                    aliases.setdefault(key, set()).add(slug)
        self.aliases = aliases
        self.canonical = canonical

    def get(self, slug: str) -> dict | None:
        return self.applications.get(slug)

    def resolve(self, value: str) -> Detection:
        if not value:
            return Detection()
        slug = value if value in self.applications else ""
        if not slug:
            key = normalize(value)
            matches = self.canonical.get(key, set()) or self.aliases.get(key, set())
            if len(matches) == 1:
                slug = next(iter(matches))
        return self.result(slug, 1.0, "manual") if slug else Detection()

    def result(self, slug: str, confidence: float, source: str) -> Detection:
        item = self.applications.get(slug)
        if not item:
            return Detection()
        return Detection(slug, item.get("name", slug), confidence, source, item.get("icon", ""))

    def detect(self, signals: list[tuple[str, str, float]]) -> Detection:
        best = Detection()
        for source, value, weight in signals:
            key = normalize(value)
            if not key:
                continue
            exact = self.canonical.get(key, set()) or self.aliases.get(key, set())
            if len(key) >= 3 and key not in GENERIC_NAMES and len(exact) == 1 and weight > best.confidence:
                best = self.result(next(iter(exact)), weight, source)
            padded = f" {key} "
            for alias, slugs in self.aliases.items():
                if len(alias) < 4 or alias in GENERIC_NAMES or len(slugs) != 1:
                    continue
                score = max(0.0, weight - 0.10)
                if score <= best.confidence:
                    continue
                if f" {alias} " in padded:
                    best = self.result(next(iter(slugs)), score, source)
        return best


@dataclass
class RouterSource:
    router: str
    entrypoint: str
    site_type: str
    status: str


@dataclass
class Card:
    router: str
    hostname: str
    url: str
    title: str
    icon: str | None = None
    group: str = ""
    status: str = "enabled"
    entrypoint: str = ""
    site_type: str = "internal"
    externally_available: bool = False
    application: str = ""
    application_name: str = ""
    detection_confidence: float = 0.0
    detection_source: str = ""
    catalog_icon: str = ""
    sources: list[RouterSource] = field(default_factory=list)


class App:
    def __init__(self, env=None, catalog=None):
        self.env = env if env is not None else os.environ
        self.traefik = self.env.get("TRAEFIK_URL", "http://traefik:8080").rstrip("/")
        self.cache = Path(self.env.get("CACHE_DIR", "/data/icons"))
        self.cache.mkdir(parents=True, exist_ok=True)
        self.routers_file = Path(self.env.get("ROUTERS_FILE", "/data/routers.toml"))
        self.timeout = float(self.env.get("FETCH_TIMEOUT_SECONDS", "5"))
        self.cards: list[Card] = []
        self.lock = threading.Lock()
        self.last_error = ""
        self.catalog = catalog or ApplicationCatalog()
        try:
            with open(self.env.get("CONFIG_FILE", "/data/traefik-home.toml"), "rb") as stream:
                self.config = tomllib.load(stream)
        except FileNotFoundError:
            self.config = {}

    def api(self, path):
        with urllib.request.urlopen(self.traefik + path, timeout=self.timeout) as response:
            return json.load(response)

    def override(self, router, host):
        result = {}
        host_overrides = self.config.get("hosts", {})
        result.update(host_overrides.get(hostname_without_port(host), {}))
        result.update(host_overrides.get(host, {}))
        result.update(self.config.get("routers", {}).get(router, {}))
        return result

    def exposure_entrypoints(self):
        configured = self.config.get("entrypoints", {})

        def values(env_name, config_name, default):
            if env_name in self.env:
                return csv(self.env[env_name])
            value = configured.get(config_name, default)
            return csv(value) if isinstance(value, str) else list(value)

        return (
            values("INTERNAL_ENTRYPOINTS", "internal", ["web"]),
            values("EXTERNAL_ENTRYPOINTS", "external", ["websecure"]),
        )

    def entrypoint_info(self, entrypoints, allowed):
        defaults, available = [], []
        for entrypoint in entrypoints:
            name = entrypoint.get("name", "")
            if not name or name not in allowed:
                continue
            available.append(name)
            if entrypoint.get("asDefault"):
                defaults.append(name)
        requested = csv(self.env.get("DEFAULT_ENTRYPOINTS", ""))
        configured = [name for name in requested if name in available]
        return set(available), configured if requested else defaults or available

    def discover(self):
        routers = self.api("/api/http/routers")
        entrypoints = self.api("/api/entrypoints")
        internal, external = self.exposure_entrypoints()
        internal_set, external_set = set(internal), set(external)
        available, defaults = self.entrypoint_info(entrypoints, internal_set | external_set)
        cards = []
        for item in routers:
            name = item.get("name", "")
            host_match = HOST_RE.search(item.get("rule", ""))
            if not name or not host_match:
                continue
            host = host_match.group(1)
            override = self.override(name, host)
            if override.get("hide", False):
                continue
            router_entrypoints = item.get("entryPoints") or defaults
            eligible = [entrypoint for entrypoint in router_entrypoints if entrypoint in available]
            if not eligible:
                continue
            internal_eps = [entrypoint for entrypoint in eligible if entrypoint in internal_set]
            external_eps = [entrypoint for entrypoint in eligible if entrypoint in external_set]
            configured = override.get("entrypoint", "")
            selected = configured if configured in eligible else (internal_eps or external_eps)[0]
            path_match = PATH_RE.search(item.get("rule", ""))
            route_path = path_match.group(1) if path_match else ""
            if route_path and not route_path.startswith("/"):
                route_path = "/" + route_path
            site_type = "both" if internal_eps and external_eps else "internal" if internal_eps else "external"
            status = item.get("status", "enabled")
            cards.append(Card(
                router=name,
                hostname=host,
                url=f"https://{host}{route_path}",
                title=override.get("title", name.split("@", 1)[0]),
                icon=override.get("icon"),
                group=override.get("group", ""),
                status=status,
                entrypoint=selected,
                site_type=site_type,
                externally_available=bool(external_eps),
                sources=[RouterSource(name, selected, site_type, status)],
            ))
        cards = self.merge_exposure_duplicates(cards)
        external_hosts = {hostname_without_port(card.hostname) for card in cards if card.externally_available}
        for card in cards:
            if hostname_without_port(card.hostname) in external_hosts:
                card.externally_available = True
        return sorted(cards, key=lambda card: (card.group.lower(), card.title.lower(), card.router.lower()))

    @staticmethod
    def merge_exposure_duplicates(cards):
        """Collapse only cross-exposure duplicates with the same effective URL."""
        by_url = {}
        for card in cards:
            by_url.setdefault(route_url_key(card.url), []).append(card)
        merged = []
        for matches in by_url.values():
            internal = [card for card in matches if card.site_type in {"internal", "both"}]
            external_only = [card for card in matches if card.site_type == "external"]
            if not internal or not external_only:
                merged.extend(matches)
                continue
            internal.sort(key=lambda card: (card.site_type != "internal", card.router.lower()))
            representative = internal[0]
            for external in external_only:
                representative.sources.extend(external.sources or [RouterSource(
                    external.router, external.entrypoint, external.site_type, external.status
                )])
            for card in internal:
                card.site_type = "both"
                card.externally_available = True
            merged.extend(internal)
        return merged

    def fetch(self, url):
        request = urllib.request.Request(url, headers={"User-Agent": "traefik-home/3"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read(2_000_000), response.headers.get_content_type(), response.geturl()

    def page_metadata(self, card):
        body, content_type, final_url = self.fetch(card.url)
        parser = PageParser()
        if content_type in ("text/html", "application/xhtml+xml"):
            parser.feed(body.decode("utf-8", "replace"))
        signals = []
        if parser.title:
            signals.append(("html-title", parser.title, 0.90))
        weights = {"application-name": 0.99, "apple-mobile-web-app-title": 0.97, "og:site_name": 0.96, "generator": 0.92}
        signals.extend((source, value, weights[source]) for source, value in parser.metadata)
        candidates = [IconCandidate(
            urllib.parse.urljoin(final_url, item["href"]), item["source"], item["size"], item["type"]
        ) for item in parser.icons]
        for manifest_href in parser.manifests[:1]:
            try:
                manifest_url = urllib.parse.urljoin(final_url, manifest_href)
                manifest_body, _, manifest_final = self.fetch(manifest_url)
                manifest = json.loads(manifest_body.decode("utf-8", "replace"))
                for key in ("name", "short_name"):
                    if manifest.get(key):
                        signals.append(("manifest-name", manifest[key], 0.99 if key == "name" else 0.97))
                for icon in manifest.get("icons", []):
                    if icon.get("src"):
                        candidates.append(IconCandidate(
                            urllib.parse.urljoin(manifest_final, icon["src"]),
                            "manifest",
                            declared_icon_size(icon.get("sizes", "")),
                            icon.get("type", "").lower(),
                        ))
            except (OSError, ValueError, TypeError):
                pass
        host = hostname_without_port(card.hostname)
        signals.extend(("hostname", label, 0.65) for label in host.split(".") if label)
        signals.append(("router-name", card.router.split("@", 1)[0], 0.45))
        unique = {candidate.url: candidate for candidate in candidates}
        return parser.title, signals, sorted(unique.values(), key=lambda candidate: candidate.score, reverse=True)

    def resolve_configured_icon(self, value):
        if not value or not value.startswith("catalog:"):
            return value
        detection = self.catalog.resolve(value.removeprefix("catalog:"))
        return f"/catalog-icons/{detection.icon}" if detection.icon else None

    def cache_remote_icon(self, card, key, candidates):
        for candidate in candidates:
            try:
                data, content_type, _ = self.fetch(candidate.url)
                extension = Path(urllib.parse.urlparse(candidate.url).path).suffix.lower()
                if not data or not (content_type.startswith("image/") or content_type == "application/octet-stream"):
                    continue
                suffix = mimetypes.guess_extension(content_type) or extension or ".ico"
                destination = self.cache / f"{key}{suffix}"
                destination.write_bytes(data)
                return "/icons/" + destination.name
            except (OSError, ValueError):
                pass
        return None

    def useful_title(self, title):
        return bool(title and normalize(title) not in GENERIC_NAMES)

    def apply_cached(self, card, cached, override):
        manual = self.catalog.resolve(override.get("application", ""))
        if manual.slug:
            card.application, card.application_name = manual.slug, manual.name
            card.detection_confidence, card.detection_source, card.catalog_icon = 1.0, "manual", manual.icon
        else:
            card.application = cached.get("application", "")
            card.application_name = cached.get("application_name", "")
            card.detection_confidence = cached.get("detection_confidence", 0.0)
            card.detection_source = cached.get("detection_source", "")
            card.catalog_icon = cached.get("catalog_icon", "")
        card.title = override.get("title", cached.get("title") or card.application_name or card.title)
        configured_icon = self.resolve_configured_icon(override.get("icon"))
        card.icon = configured_icon or cached.get("icon")
        if not card.icon and card.catalog_icon:
            card.icon = "/catalog-icons/" + card.catalog_icon

    def enrich(self, card):
        key = hashlib.sha256(card.url.encode()).hexdigest()[:24]
        metadata_file = self.cache / f"{key}.json"
        override = self.override(card.router, card.hostname)
        try:
            cached = json.loads(metadata_file.read_text("utf-8"))
        except (OSError, ValueError):
            cached = {}
        max_age = int(self.env.get("METADATA_REFRESH_SECONDS", "86400"))
        if cached.get("schema") == CACHE_VERSION and time.time() - cached.get("updated", 0) < max_age:
            self.apply_cached(card, cached, override)
            return
        page_title, signals, candidates = "", [], []
        try:
            page_title, signals, candidates = self.page_metadata(card)
        except (OSError, ValueError):
            host = hostname_without_port(card.hostname)
            signals = [("hostname", label, 0.65) for label in host.split(".") if label]
            signals.append(("router-name", card.router.split("@", 1)[0], 0.45))
        manual = self.catalog.resolve(override.get("application", ""))
        detection = manual if manual.slug else self.catalog.detect(signals)
        card.application, card.application_name = detection.slug, detection.name
        card.detection_confidence, card.detection_source, card.catalog_icon = detection.confidence, detection.source, detection.icon
        if override.get("title"):
            card.title = override["title"]
        elif self.useful_title(page_title):
            card.title = page_title
        elif detection.name:
            card.title = detection.name
        configured_icon = self.resolve_configured_icon(override.get("icon"))
        icon_path = configured_icon
        site_svg = [candidate for candidate in candidates if candidate.is_svg]
        high_quality_raster = [candidate for candidate in candidates if candidate.high_quality and not candidate.is_svg]
        ordinary = [candidate for candidate in candidates if not candidate.high_quality]
        if not icon_path:
            icon_path = self.cache_remote_icon(card, key, site_svg)
        catalog_svg = detection.icon.lower().endswith(".svg")
        if not icon_path and catalog_svg and detection.confidence >= 0.60:
            icon_path = "/catalog-icons/" + detection.icon
        if not icon_path and not manual.slug:
            icon_path = self.cache_remote_icon(card, key, high_quality_raster)
        if not icon_path and detection.icon and detection.confidence >= 0.60:
            icon_path = "/catalog-icons/" + detection.icon
        if not icon_path:
            icon_path = self.cache_remote_icon(card, key, ordinary)
        if not icon_path:
            fallback = IconCandidate(urllib.parse.urljoin(card.url, "/favicon.ico"), "favicon")
            icon_path = self.cache_remote_icon(card, key, [fallback])
        card.icon = icon_path
        metadata_file.write_text(json.dumps({
            "schema": CACHE_VERSION,
            "updated": time.time(),
            "title": card.title,
            "icon": card.icon,
            "application": card.application,
            "application_name": card.application_name,
            "detection_confidence": card.detection_confidence,
            "detection_source": card.detection_source,
            "catalog_icon": card.catalog_icon,
        }), "utf-8")

    @staticmethod
    def toml_string(value):
        return json.dumps(str(value), ensure_ascii=False)

    def inventory_text(self, cards):
        source = self.catalog.source
        lines = [
            "# Generated by traefik-home. Manual settings belong in traefik-home.toml.",
            "# This file is replaced atomically after every successful discovery refresh.",
            "",
            "[catalog]",
            f"repository = {self.toml_string(source.get('repository', ''))}",
            f"commit = {self.toml_string(source.get('commit', ''))}",
            "",
        ]
        inventory = []
        for card in cards:
            sources = card.sources or [RouterSource(card.router, card.entrypoint, card.site_type, card.status)]
            inventory.extend((source, card) for source in sources)
        for source, card in sorted(inventory, key=lambda item: item[0].router):
            lines.extend([
                f"[routers.{self.toml_string(source.router)}]",
                f"hostname = {self.toml_string(card.hostname)}",
                f"url = {self.toml_string(card.url)}",
                f"entrypoint = {self.toml_string(source.entrypoint)}",
                f"exposure = {self.toml_string(source.site_type)}",
                f"externally_available = {str(card.externally_available).lower()}",
                f"status = {self.toml_string(source.status)}",
                f"title = {self.toml_string(card.title)}",
                f"application = {self.toml_string(card.application or 'unknown')}",
                f"detection_confidence = {card.detection_confidence:.2f}",
                f"detection_source = {self.toml_string(card.detection_source or 'none')}",
                f"icon = {self.toml_string('catalog:' + card.application if card.catalog_icon and card.icon and card.icon.startswith('/catalog-icons/') else card.icon or '')}",
                "",
            ])
        return "\n".join(lines)

    def write_inventory(self, cards):
        content = self.inventory_text(cards)
        self.routers_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.routers_file.read_text("utf-8") == content:
                return
        except FileNotFoundError:
            pass
        temporary = self.routers_file.with_name(self.routers_file.name + ".tmp")
        temporary.write_text(content, "utf-8")
        os.replace(temporary, self.routers_file)

    def refresh(self):
        try:
            cards = self.discover()
            for card in cards:
                self.enrich(card)
            self.write_inventory(cards)
            with self.lock:
                self.cards = cards
            self.last_error = ""
        except (OSError, ValueError) as error:
            self.last_error = str(error)

    def loop(self):
        while True:
            time.sleep(int(self.env.get("REFRESH_SECONDS", "60")))
            self.refresh()

    def render(self):
        with self.lock:
            cards = list(self.cards)
        ui = self.config.get("ui", {})
        target = "_blank" if ui.get("open_in_new_tab", False) else "_self"
        parts, group = [], None
        for card in cards:
            if card.group and card.group != group:
                parts.append(f'<h2 class="col-12 group">{html.escape(card.group)}</h2>')
            group = card.group
            icon = (
                f'<div style="background-image:url(&quot;{html.escape(card.icon, quote=True)}&quot;)" class="col mb-3 icon"></div>'
                if card.icon else
                f'<div data-name="{html.escape(card.title[:2])}" class="col mb-3 initials"></div>'
            )
            dot = (
                f'<span data-running="{str(card.status == "enabled").lower()}" class="me-2"></span>'
                if ui.get("show_status", True) else ""
            )
            external = (
                '<span class="badge rounded-pill bg-info text-dark ms-2" title="Externally available" '
                'aria-label="Externally available">External</span>'
                if card.externally_available else ""
            )
            parts.append(
                f'<div class="col-xl-2 col-lg-3 col-md-4 col-sm-6 col-6 mt-4 mx-xl-2">'
                f'<a class="text-decoration-none text-secondary" target="{target}" href="{html.escape(card.url, quote=True)}" '
                f'data-router="{html.escape(card.router, quote=True)}" data-entrypoint="{html.escape(card.entrypoint, quote=True)}" '
                f'data-site-type="{card.site_type}" data-application="{html.escape(card.application, quote=True)}">'
                f'<div class="row-cols-1 text-center">{icon}<span class="col">{dot}{html.escape(card.title)}{external}</span></div></a></div>'
            )
        return (STATIC / "index.html").read_text("utf-8").replace("<!-- CARDS -->", "\n".join(parts)).encode()


class Handler(BaseHTTPRequestHandler):
    app: App

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            return self.send_body(200, self.app.render(), "text/html; charset=utf-8")
        if path == "/health":
            return self.send_body(200, b"ok", "text/plain")
        root, name = (self.app.cache, path[7:]) if path.startswith("/icons/") else (STATIC, path.lstrip("/"))
        root = root.resolve()
        candidate = (root / name).resolve()
        if candidate != root and root not in candidate.parents:
            return self.send_body(404, b"not found", "text/plain")
        if not candidate.is_file():
            return self.send_body(404, b"not found", "text/plain")
        self.send_body(200, candidate.read_bytes(), mimetypes.guess_type(candidate)[0] or "application/octet-stream")

    def send_body(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    app = App()
    app.refresh()
    Handler.app = app
    threading.Thread(target=app.loop, daemon=True).start()
    ThreadingHTTPServer(("", int(os.getenv("PORT", "80"))), Handler).serve_forever()


if __name__ == "__main__":
    main()
