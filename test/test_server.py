import os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))
from server import App, Card

class DiscoveryTests(unittest.TestCase):
    def app(self, routers, entrypoints=None, config=""):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        cfg=Path(tmp.name)/"config.toml"; cfg.write_text(config,encoding="utf-8")
        env={"CACHE_DIR":tmp.name,"CONFIG_FILE":str(cfg),"FETCH_TIMEOUT_SECONDS":".1"}
        app=App(env); app.api=lambda path: routers if path.endswith("routers") else (entrypoints or [{"name":"web","address":":80"},{"name":"websecure","address":":443"}])
        return app

    def test_explicit_default_rule_and_default_entrypoints(self):
        routers=[
          {"name":"labelled@docker","rule":"Host(`labelled.test`)","entryPoints":["web"]},
          {"name":"whoami@docker","rule":"Host(`whoami.test`)","entryPoints":["web"]}, # effective providers.docker.defaultRule
          {"name":"implicit@docker","rule":"Host(`implicit.test`)","entryPoints":[]},
        ]
        cards=self.app(routers,[{"name":"web","address":":80","asDefault":True}]).discover()
        self.assertEqual([c.url for c in cards],["http://implicit.test","http://labelled.test","http://whoami.test"])

    def test_gluetun_routers_stay_separate_and_unrelated_instances_survive(self):
        routers=[
          {"name":"qbittorrent@docker","rule":"Host(`qbit.test`) && PathPrefix(`/ui`)","entryPoints":["web"]},
          {"name":"sabnzbd@docker","rule":"Host(`sab.test`)","entryPoints":["web"]},
          {"name":"blue@docker","rule":"Host(`one.plex.test`)","entryPoints":["web"]},
          {"name":"completely-unrelated@docker","rule":"Host(`two.plex.test`)","entryPoints":["web"]},
        ]
        cards=self.app(routers).discover()
        self.assertEqual(len(cards),4); self.assertIn("http://qbit.test/ui",[c.url for c in cards])
        self.assertIn("http://one.plex.test",[c.url for c in cards]); self.assertIn("http://two.plex.test",[c.url for c in cards])

    def test_filter_and_router_hostname_overrides(self):
        routers=[{"name":"hide@docker","rule":"Host(`hide.test`)","entryPoints":["web"]},{"name":"app@docker","rule":"Host(`app.test`)","entryPoints":["web"]}]
        config='''[overrides.router."hide@docker"]\nhide=true\n[overrides.hostname."app.test"]\ntitle="Host title"\ngroup="Tools"\n[overrides.router."app@docker"]\ntitle="Exact title"\nicon="/mine.svg"\n'''
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
    def test_failure_uses_generic_initials_and_is_cached(self):
        app=self.app(); calls=0
        def fail(url):
            nonlocal calls; calls+=1; raise OSError("nope")
        app.fetch=fail; card=Card("generic@docker","x.test","http://x.test","Generic"); app.enrich(card); app.enrich(card)
        self.assertIsNone(card.icon)
        app.cards=[card]
        self.assertIn('class="col mb-3 initials"',app.render().decode())
        self.assertEqual(calls,1)

if __name__ == "__main__": unittest.main()
