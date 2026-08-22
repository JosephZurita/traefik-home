#!/usr/bin/env python3
"""Traefik runtime-API driven homepage, with server-side metadata caching."""
from __future__ import annotations
import hashlib, html, json, mimetypes, os, re, threading, time, tomllib
import urllib.parse, urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATIC = next(p for p in (Path(__file__).parent / "static", Path(__file__).parent.parent / "static") if p.exists())
HOST_RE = re.compile(r"Host\s*\(\s*[`\"']([^`\"']+)", re.I)
PATH_RE = re.compile(r"Path(?:Prefix)?\s*\(\s*[`\"']([^`\"']+)", re.I)

class PageParser(HTMLParser):
    def __init__(self): super().__init__(); self.inside=False; self.parts=[]; self.icons=[]
    def handle_starttag(self, tag, attrs):
        attrs={k.lower():v for k,v in attrs if v is not None}
        if tag.lower()=="title": self.inside=True
        rel=set(attrs.get("rel","").lower().split())
        if tag.lower()=="link" and attrs.get("href") and ("icon" in rel or "apple-touch-icon" in rel):
            self.icons.append((1 if "apple-touch-icon" in rel else 0, attrs["href"]))
    def handle_endtag(self, tag):
        if tag.lower()=="title": self.inside=False
    def handle_data(self, data):
        if self.inside: self.parts.append(data)
    @property
    def title(self): return " ".join("".join(self.parts).split())

@dataclass
class Card:
    router:str; hostname:str; url:str; title:str; icon:str|None=None; group:str=""; status:str="enabled"

def csv(value): return [x.strip() for x in value.split(",") if x.strip()]

class App:
    def __init__(self, env=None):
        self.env=env or os.environ; self.traefik=self.env.get("TRAEFIK_URL","http://traefik:8080").rstrip("/")
        self.cache=Path(self.env.get("CACHE_DIR","/data/icons")); self.cache.mkdir(parents=True,exist_ok=True)
        self.timeout=float(self.env.get("FETCH_TIMEOUT_SECONDS","5")); self.cards=[]; self.lock=threading.Lock(); self.last_error=""
        try:
            with open(self.env.get("CONFIG_FILE","/config/traefik-home.toml"),"rb") as f: self.config=tomllib.load(f)
        except FileNotFoundError: self.config={}
    def api(self,path):
        with urllib.request.urlopen(self.traefik+path,timeout=self.timeout) as r:return json.load(r)
    def override(self,router,host):
        o=self.config.get("overrides",{}); result={}
        result.update(o.get("hostname",{}).get(host,{})); result.update(o.get("router",{}).get(router,{})); return result
    def entrypoint_info(self, entrypoints):
        http=set(csv(self.env.get("HTTP_ENTRYPOINTS","web"))); https=set(csv(self.env.get("HTTPS_ENTRYPOINTS","websecure")))
        schemes={}; defaults=[]
        for ep in entrypoints:
            name=ep.get("name",""); schemes[name]="https" if name in https or re.search(r":443(?:/|$)",ep.get("address","")) else "http"
            if ep.get("asDefault"): defaults.append(name)
        schemes.update({x:"http" for x in http}); schemes.update({x:"https" for x in https})
        return schemes, csv(self.env.get("DEFAULT_ENTRYPOINTS","")) or defaults or list(schemes)
    def discover(self):
        routers=self.api("/api/http/routers")
        try: entrypoints=self.api("/api/entrypoints")
        except (OSError,ValueError): entrypoints=[]
        schemes,defaults=self.entrypoint_info(entrypoints); cards=[]
        for item in routers:
            name=item.get("name",""); match=HOST_RE.search(item.get("rule",""))
            if not name or not match: continue
            host=match.group(1); override=self.override(name,host)
            if override.get("hide",False): continue
            eps=item.get("entryPoints") or defaults; ep=eps[0] if eps else ""
            scheme="https" if item.get("tls") is not None else schemes.get(ep,"http")
            path=PATH_RE.search(item.get("rule","")); path=path.group(1) if path else ""
            if path and not path.startswith("/"): path="/"+path
            cards.append(Card(name,host,f"{scheme}://{host}{path}",override.get("title",name.split("@",1)[0]),override.get("icon"),override.get("group",""),item.get("status","enabled")))
        return sorted(cards,key=lambda c:(c.group.lower(),c.title.lower(),c.router.lower()))
    def fetch(self,url):
        request=urllib.request.Request(url,headers={"User-Agent":"traefik-home/2"})
        with urllib.request.urlopen(request,timeout=self.timeout) as r:return r.read(2_000_000),r.headers.get_content_type(),r.geturl()
    def enrich(self,card):
        key=hashlib.sha256(card.url.encode()).hexdigest()[:24]; meta=self.cache/(key+".json"); override=self.override(card.router,card.hostname)
        try: cached=json.loads(meta.read_text("utf-8"))
        except (OSError,ValueError): cached={}
        max_age=int(self.env.get("METADATA_REFRESH_SECONDS","86400"))
        if cached and time.time()-cached.get("updated",0)<max_age:
            card.title=override.get("title",cached.get("title") or card.title); card.icon=card.icon or cached.get("icon"); return
        title=""; icon_path=None
        try:
            body,ctype,final=self.fetch(card.url); parser=PageParser()
            if ctype in ("text/html","application/xhtml+xml"): parser.feed(body.decode("utf-8","replace")); title=parser.title
            candidates=[urllib.parse.urljoin(final,u) for _,u in sorted(parser.icons)]+[urllib.parse.urljoin(final,"/favicon.ico")]
            for candidate in candidates:
                try:
                    data,itype,_=self.fetch(candidate)
                    if not data or not (itype.startswith("image/") or Path(urllib.parse.urlparse(candidate).path).suffix.lower() in (".ico",".svg",".png")): continue
                    suffix=mimetypes.guess_extension(itype) or Path(urllib.parse.urlparse(candidate).path).suffix or ".ico"
                    dest=self.cache/(key+suffix); dest.write_bytes(data); icon_path="/icons/"+dest.name; break
                except (OSError,ValueError): pass
        except (OSError,ValueError): pass
        card.title=override.get("title",title or card.title); card.icon=override.get("icon",icon_path)
        meta.write_text(json.dumps({"updated":time.time(),"title":title,"icon":icon_path}),"utf-8")
    def refresh(self):
        try:
            cards=self.discover()
            for card in cards:self.enrich(card)
            with self.lock:self.cards=cards
            self.last_error=""
        except (OSError,ValueError) as e:self.last_error=str(e)
    def loop(self):
        while True:self.refresh();time.sleep(int(self.env.get("REFRESH_SECONDS","60")))
    def render(self):
        with self.lock: cards=list(self.cards)
        ui=self.config.get("ui",{}); target="_blank" if ui.get("open_in_new_tab",False) else "_self"; parts=[]; group=None
        for c in cards:
            if c.group and c.group!=group:parts.append(f'<h2 class="col-12 group">{html.escape(c.group)}</h2>')
            group=c.group
            icon=(f'<div style="background-image:url(&quot;{html.escape(c.icon,quote=True)}&quot;)" class="col mb-3 icon"></div>' if c.icon else f'<div data-name="{html.escape(c.title[:2])}" class="col mb-3 initials"></div>')
            dot=f'<span data-running="{str(c.status=="enabled").lower()}" class="me-2"></span>' if ui.get("show_status",True) else ""
            parts.append(f'<div class="col-xl-2 col-lg-3 col-md-4 col-sm-6 col-6 mt-4 mx-xl-2"><a class="text-decoration-none text-secondary" target="{target}" href="{html.escape(c.url,quote=True)}" data-router="{html.escape(c.router,quote=True)}"><div class="row-cols-1 text-center">{icon}<span class="col">{dot}{html.escape(c.title)}</span></div></a></div>')
        return (STATIC/"index.html").read_text("utf-8").replace("<!-- CARDS -->","\n".join(parts)).encode()

class Handler(BaseHTTPRequestHandler):
    app:App
    def do_GET(self):
        path=urllib.parse.urlparse(self.path).path
        if path=="/":return self.send_body(200,self.app.render(),"text/html; charset=utf-8")
        if path=="/health":return self.send_body(200,b"ok","text/plain")
        root,name=(self.app.cache,path[7:]) if path.startswith("/icons/") else (STATIC,path.lstrip("/")); file=root/Path(name).name
        if not file.is_file():return self.send_body(404,b"not found","text/plain")
        self.send_body(200,file.read_bytes(),mimetypes.guess_type(file)[0] or "application/octet-stream")
    def send_body(self,status,body,ctype):
        self.send_response(status);self.send_header("Content-Type",ctype);self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
    def log_message(self,*args):pass

def main():
    app=App();app.refresh();Handler.app=app;threading.Thread(target=app.loop,daemon=True).start()
    ThreadingHTTPServer(("",int(os.getenv("PORT","80"))),Handler).serve_forever()
if __name__=="__main__":main()
