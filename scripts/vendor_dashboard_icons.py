#!/usr/bin/env python3
"""Build the offline application catalog from a pinned Dashboard Icons checkout."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

SOURCE_REPOSITORY = "https://github.com/homarr-labs/dashboard-icons"
SOURCE_COMMIT = "8223c9cd5b44418357800d45bb78c3956dd83ba3"
LARGE_SVG_BYTES = 100_000
ACRONYMS = {"ai", "api", "aws", "dns", "git", "gpu", "http", "https", "ip", "llm", "nas", "os", "pdf", "sql", "ssh", "ssl", "ui", "vpn"}


def display_name(slug: str) -> str:
    words = slug.replace("_", "-").split("-")
    return " ".join(word.upper() if word in ACRONYMS else word.capitalize() for word in words)


def choose_asset(source: Path, slug: str, metadata: dict) -> Path:
    names = [slug, *dict.fromkeys((metadata.get("colors") or {}).values())]
    svg = [source / "svg" / f"{name}.svg" for name in names]
    webp = [source / "webp" / f"{name}.webp" for name in names]
    svg = next((path for path in svg if path.is_file()), None)
    webp = next((path for path in webp if path.is_file()), None)
    if svg and (svg.stat().st_size <= LARGE_SVG_BYTES or not webp):
        return svg
    if webp:
        return webp
    if svg:
        return svg
    raise FileNotFoundError(f"No catalog asset for {slug}")


def build(source: Path, catalog_dir: Path, icon_dir: Path) -> None:
    revision = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if revision != SOURCE_COMMIT:
        raise SystemExit(f"Expected Dashboard Icons {SOURCE_COMMIT}, found {revision}")
    metadata = json.loads((source / "metadata.json").read_text("utf-8"))
    catalog_dir.mkdir(parents=True, exist_ok=True)
    icon_dir.mkdir(parents=True, exist_ok=True)
    applications = {}
    expected_icons = set()
    for slug in sorted(metadata):
        item = metadata[slug]
        asset = choose_asset(source, slug, item)
        destination_name = slug + asset.suffix.lower()
        shutil.copyfile(asset, icon_dir / destination_name)
        expected_icons.add(destination_name)
        aliases = [display_name(slug), *item.get("aliases", [])]
        applications[slug] = {
            "name": display_name(slug),
            "aliases": list(dict.fromkeys(alias.strip() for alias in aliases if alias.strip())),
            "categories": item.get("categories", []),
            "icon": destination_name,
        }
    for path in icon_dir.iterdir():
        if path.is_file() and path.name not in expected_icons:
            path.unlink()
    catalog = {
        "source": {"repository": SOURCE_REPOSITORY, "commit": SOURCE_COMMIT, "license": "Apache-2.0"},
        "applications": applications,
    }
    (catalog_dir / "applications.json").write_text(json.dumps(catalog, ensure_ascii=False, separators=(",", ":")), "utf-8")
    shutil.copyfile(source / "LICENSE", catalog_dir / "DASHBOARD_ICONS_LICENSE.txt")
    (catalog_dir / "README.md").write_text(
        "# Offline application catalog\n\n"
        f"Generated from [{SOURCE_REPOSITORY}]({SOURCE_REPOSITORY}) at commit `{SOURCE_COMMIT}`.\n\n"
        "The catalog metadata and icons are distributed under the included Apache-2.0 license. "
        "Product names and trademarks remain the property of their respective owners.\n",
        "utf-8",
    )
    print(f"Vendored {len(applications)} applications into {icon_dir}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path, help="Pinned Dashboard Icons checkout")
    parser.add_argument("--catalog-dir", type=Path, default=Path("app/catalog"))
    parser.add_argument("--icon-dir", type=Path, default=Path("static/catalog-icons"))
    args = parser.parse_args()
    build(args.source.resolve(), args.catalog_dir.resolve(), args.icon_dir.resolve())


if __name__ == "__main__":
    main()
