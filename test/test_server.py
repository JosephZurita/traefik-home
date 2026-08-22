import os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))
from server import App, ApplicationCatalog, Card

class DiscoveryTests(unittest.TestCase):
    def app(self, routers, entrypoints=None, config="", extra_env=None):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        cfg=Path(tmp.name)/"config.toml"; cfg.write_text(config,encoding="utf-8")
        env={"CACHE_DIR":tmp.name,"CONFIG_FILE":str(cfg),"FETCH_TIMEOUT_SECONDS":".1"}
        env.update(extra_env or {})
        app=App(env); app.api=lambda path: routers if path.endswith("routers") else (entrypoints if entrypoints is not None else [{"name":"web","address":":80"},{"name":"websecure","address":":443"}])
        return app

    def test_default_config_lives_in_data_volume(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        with patch("builtins.open",side_effect=FileNotFoundError) as opened:
            App({"CACHE_DIR":tmp.name})
        opened.assert_called_once_with("/data/traefik-home.toml","rb")

    def test_explicit_default_rule_and_default_entrypoints(self):
        routers=[
          {"name":"labelled@docker","rule":"Host(`labelled.test`)","entryPoints":["web"]},
          {"name":"whoami@docker","rule":"Host(`whoami.test`)","entryPoints":["web"]}, # effective providers.docker.defaultRule
          {"name":"implicit@docker","rule":"Host(`implicit.test`)","entryPoints":[]},
        ]
        cards=self.app(routers,[{"name":"web","address":":80","asDefault":True}]).discover()
        self.assertEqual([c.url for c in cards],["https://implicit.test","https://labelled.test","https://whoami.test"])

    def test_removed_entrypoint_is_not_used(self):
        routers=[{"name":"stale@docker","rule":"Host(`stale.test`)","entryPoints":["removed"]}]
        self.assertEqual(self.app(routers,[{"name":"web","address":":80"}]).discover(),[])

    def test_removed_entrypoint_is_filtered_when_router_has_a_live_one(self):
        routers=[{"name":"mixed@docker","rule":"Host(`mixed.test`)","entryPoints":["removed","lan"]}]
        cards=self.app(routers,[{"name":"lan","address":":8080"}],extra_env={"INTERNAL_ENTRYPOINTS":"lan","EXTERNAL_ENTRYPOINTS":""}).discover()
        self.assertEqual([c.url for c in cards],["https://mixed.test"])

    def test_empty_live_entrypoints_do_not_manufacture_entrypoints(self):
        routers=[{"name":"implicit@docker","rule":"Host(`implicit.test`)","entryPoints":[]}]
        self.assertEqual(self.app(routers,[]).discover(),[])

    def test_removed_explicit_default_does_not_fall_back(self):
        routers=[{"name":"implicit@docker","rule":"Host(`implicit.test`)","entryPoints":[]}]
        app=self.app(routers,[{"name":"web","address":":80"}],extra_env={"DEFAULT_ENTRYPOINTS":"removed"})
        self.assertEqual(app.discover(),[])

    def test_internal_external_allowlist_and_hostname_indicator_ignore_port(self):
        routers=[
          {"name":"internal@docker","rule":"Host(`SAME.test:8080`)","entryPoints":["lan"]},
          {"name":"external@docker","rule":"Host(`same.test:443`)","entryPoints":["wan"]},
          {"name":"private@docker","rule":"Host(`private.test:8080`)","entryPoints":["lan"]},
          {"name":"ignored@docker","rule":"Host(`ignored.test`)","entryPoints":["admin"]},
        ]
        entrypoints=[{"name":"lan","address":":8080"},{"name":"wan","address":":443"},{"name":"admin","address":":9000"}]
        env={"INTERNAL_ENTRYPOINTS":"lan","EXTERNAL_ENTRYPOINTS":"wan"}
        app=self.app(routers,entrypoints,extra_env=env); cards=app.discover(); by_router={c.router:c for c in cards}
        self.assertEqual(set(by_router),{"internal@docker","external@docker","private@docker"})
        self.assertTrue(by_router["internal@docker"].externally_available)
        self.assertTrue(by_router["external@docker"].externally_available)
        self.assertFalse(by_router["private@docker"].externally_available)
        app.cards=cards; page=app.render().decode()
        self.assertEqual(page.count('aria-label="Externally available"'),2)

    def test_router_on_both_types_prefers_internal_link(self):
        routers=[{"name":"both@docker","rule":"Host(`both.test`)","entryPoints":["wan","lan"]}]
        entrypoints=[{"name":"lan","address":":80"},{"name":"wan","address":":443"}]
        env={"INTERNAL_ENTRYPOINTS":"lan","EXTERNAL_ENTRYPOINTS":"wan"}
        card=self.app(routers,entrypoints,extra_env=env).discover()[0]
        self.assertEqual((card.entrypoint,card.site_type,card.url,card.externally_available),("lan","both","https://both.test",True))

    def test_router_config_can_select_an_assigned_entrypoint(self):
        routers=[{"name":"both@docker","rule":"Host(`both.test`)","entryPoints":["wan","lan"]}]
        entrypoints=[{"name":"lan","address":":80"},{"name":"wan","address":":443"}]
        config='''[entrypoints]\ninternal=["lan"]\nexternal=["wan"]\n[routers."both@docker"]\nentrypoint="wan"\n'''
        card=self.app(routers,entrypoints,config=config).discover()[0]
        self.assertEqual(card.entrypoint,"wan")

    def test_invalid_router_entrypoint_falls_back_to_live_internal(self):
        routers=[{"name":"both@docker","rule":"Host(`both.test`)","entryPoints":["wan","lan"]}]
        entrypoints=[{"name":"lan","address":":80"},{"name":"wan","address":":443"}]
        config='''[entrypoints]\ninternal=["lan"]\nexternal=["wan"]\n[routers."both@docker"]\nentrypoint="removed"\n'''
        card=self.app(routers,entrypoints,config=config).discover()[0]
        self.assertEqual(card.entrypoint,"lan")

    def test_toml_entrypoint_types_and_environment_precedence(self):
        config='''[entrypoints]\ninternal=["lan"]\nexternal=["wan"]\n'''
        app=self.app([],config=config)
        self.assertEqual(app.exposure_entrypoints(),(["lan"],["wan"]))
        app.env.update({"INTERNAL_ENTRYPOINTS":"private","EXTERNAL_ENTRYPOINTS":"public"})
        self.assertEqual(app.exposure_entrypoints(),(["private"],["public"]))

    def test_gluetun_routers_stay_separate_and_unrelated_instances_survive(self):
        routers=[
          {"name":"qbittorrent@docker","rule":"Host(`qbit.test`) && PathPrefix(`/ui`)","entryPoints":["web"]},
          {"name":"sabnzbd@docker","rule":"Host(`sab.test`)","entryPoints":["web"]},
          {"name":"blue@docker","rule":"Host(`one.plex.test`)","entryPoints":["web"]},
          {"name":"completely-unrelated@docker","rule":"Host(`two.plex.test`)","entryPoints":["web"]},
        ]
        cards=self.app(routers).discover()
        self.assertEqual(len(cards),4); self.assertIn("https://qbit.test/ui",[c.url for c in cards])
        self.assertIn("https://one.plex.test",[c.url for c in cards]); self.assertIn("https://two.plex.test",[c.url for c in cards])

    def test_filter_and_router_hostname_overrides(self):
        routers=[{"name":"hide@docker","rule":"Host(`hide.test`)","entryPoints":["web"]},{"name":"app@docker","rule":"Host(`APP.test:443`)","entryPoints":["web"]}]
        config='''[routers."hide@docker"]\nentrypoint="web"\nhide=true\n[hosts."app.test"]\ntitle="Host title"\ngroup="Tools"\n[routers."app@docker"]\nentrypoint="web"\ntitle="Exact title"\nicon="/mine.svg"\n'''
        cards=self.app(routers,config=config).discover()
        self.assertEqual(len(cards),1); self.assertEqual((cards[0].title,cards[0].icon,cards[0].group),("Exact title","/mine.svg","Tools"))

class FaviconTests(unittest.TestCase):
    def app(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        return App({"CACHE_DIR":tmp.name,"CONFIG_FILE":str(Path(tmp.name)/"missing"),"METADATA_REFRESH_SECONDS":"86400"})
    def test_html_title_and_relative_favicon_discovery(self):
        app=self.app(); calls=[]
        def fetch(url):
            calls.append(url)
            if len(calls)==1:return b'<title>Useful App</title><link rel="icon" href="assets/icon.png">',"text/html","http://app.test/base/"
            return b"PNG","image/png",url
        app.fetch=fetch; card=Card("odd-router@docker","app.test","http://app.test/base/","odd-router")
        app.enrich(card)
        self.assertEqual(card.title,"Useful App"); self.assertEqual(calls[1],"http://app.test/base/assets/icon.png"); self.assertTrue(card.icon.startswith("/icons/"))
    def test_favicon_ico_fallback(self):
        app=self.app(); calls=[]
        def fetch(url):
            calls.append(url)
            if len(calls)==1:return b"<title>X</title>","text/html",url
            return b"ICO","image/x-icon",url
        app.fetch=fetch; card=Card("x@docker","x.test","http://x.test/path","x"); app.enrich(card)
        self.assertEqual(calls[1],"http://x.test/favicon.ico"); self.assertIsNotNone(card.icon)
    def test_largest_page_icon_is_preferred(self):
        app=self.app(); calls=[]
        def fetch(url):
            calls.append(url)
            if len(calls)==1:return b'<title>Uncatalogued Portal</title><link rel="icon" sizes="16x16" href="small.png"><link rel="apple-touch-icon" sizes="180x180" href="touch.png">',"text/html",url
            return b"PNG","image/png",url
        app.fetch=fetch; card=Card("portal@docker","portal.invalid","http://portal.invalid","portal"); app.enrich(card)
        self.assertEqual(calls[1],"http://portal.invalid/touch.png")
    def test_manifest_name_and_high_resolution_icon(self):
        app=self.app(); calls=[]
        def fetch(url):
            calls.append(url)
            if len(calls)==1:return b'<link rel="manifest" href="/manifest.json">',"text/html",url
            if url.endswith("manifest.json"):return b'{"name":"Jellyfin","icons":[{"src":"icon-512.png","sizes":"512x512","type":"image/png"}]}',"application/manifest+json",url
            return b"PNG","image/png",url
        app.fetch=fetch; card=Card("odd@docker","unknown.invalid","http://unknown.invalid","odd"); app.enrich(card)
        self.assertEqual(card.application,"jellyfin"); self.assertEqual(card.detection_source,"manifest-name")
        self.assertEqual(calls[2],"http://unknown.invalid/icon-512.png")
    def test_catalog_icon_fallback_avoids_favicon_request(self):
        app=self.app(); calls=[]
        app.fetch=lambda url:(calls.append(url) or (b'<title>Plex</title>',"text/html",url))
        card=Card("odd@docker","unknown.invalid","http://unknown.invalid","odd"); app.enrich(card)
        self.assertEqual(card.application,"plex"); self.assertTrue(card.icon.startswith("/catalog-icons/plex.")); self.assertEqual(len(calls),1)
    def test_manual_application_and_catalog_icon_override(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        cfg=Path(tmp.name)/"config.toml"; cfg.write_text('''[routers."odd@docker"]\napplication="plex"\ntitle="Cinema"\nicon="catalog:jellyfin"\n''',encoding="utf-8")
        app=App({"CACHE_DIR":tmp.name,"CONFIG_FILE":str(cfg)}); app.fetch=lambda url:(_ for _ in ()).throw(OSError("offline"))
        card=Card("odd@docker","unknown.invalid","http://unknown.invalid","odd"); app.enrich(card)
        self.assertEqual((card.application,card.title),("plex","Cinema")); self.assertTrue(card.icon.startswith("/catalog-icons/jellyfin."))
    def test_failure_uses_generic_initials_and_is_cached(self):
        app=self.app(); calls=0
        def fail(url):
            nonlocal calls; calls+=1; raise OSError("nope")
        app.fetch=fail; card=Card("generic@docker","x.test","http://x.test","Generic"); app.enrich(card); app.enrich(card)
        self.assertIsNone(card.icon)
        app.cards=[card]
        self.assertIn('class="col mb-3 initials"',app.render().decode())
        self.assertEqual(calls,2)

class CatalogTests(unittest.TestCase):
    def test_exact_and_title_alias_detection(self):
        catalog=ApplicationCatalog()
        self.assertEqual(catalog.resolve("plex").slug,"plex")
        detected=catalog.detect([("html-title","Plex Web",.9)])
        self.assertEqual(detected.slug,"plex"); self.assertGreaterEqual(detected.confidence,.8)
    def test_ambiguous_generic_name_stays_unknown(self):
        detected=ApplicationCatalog().detect([("html-title","Media Server",.9)])
        self.assertEqual(detected.slug,"")

class InventoryTests(unittest.TestCase):
    def test_generated_inventory_has_entrypoint_for_every_router(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        inventory=Path(tmp.name)/"routers.toml"
        app=App({"CACHE_DIR":str(Path(tmp.name)/"icons"),"CONFIG_FILE":str(Path(tmp.name)/"missing"),"ROUTERS_FILE":str(inventory)})
        cards=[Card("one@docker","one.test","https://one.test","One",entrypoint="lan",application="plex",application_name="Plex",detection_confidence=.9,detection_source="html-title",catalog_icon="plex.svg")]
        app.write_inventory(cards)
        with inventory.open("rb") as stream:data=__import__("tomllib").load(stream)
        self.assertEqual(data["routers"]["one@docker"]["entrypoint"],"lan")
        self.assertEqual(data["routers"]["one@docker"]["application"],"plex")
        self.assertFalse(Path(str(inventory)+".tmp").exists())

if __name__ == "__main__": unittest.main()
