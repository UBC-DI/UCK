#!/usr/bin/env python3
"""Copie les dernières publications de la Page Facebook de l'UCK-G dans news.json.

Variables d'environnement :
  FB_PAGE_ID      identifiant numérique de la Page          (obligatoire)
  FB_PAGE_TOKEN   jeton d'accès de la Page                  (obligatoire)
  GRAPH_VERSION   version de l'API, ex. v26.0               (facultatif)
  GRAPH_BASE      adresse de l'API (pour les tests)         (facultatif)
  MAX_ITEMS       nombre de publications gardées, défaut 30 (facultatif)

Règles de sécurité :
  - si l'API échoue, news.json n'est PAS modifié (code de sortie 1) ;
  - les actualités ajoutées à la main (source != "facebook") sont conservées ;
  - les images sont copiées dans media/ (les liens d'images de Facebook expirent).
"""
import json, os, sys, re, time, urllib.request, urllib.parse, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWS = os.path.join(ROOT, "news.json")
MEDIA = os.path.join(ROOT, "media")
BASE = os.environ.get("GRAPH_BASE", "https://graph.facebook.com").rstrip("/")
VERSION = os.environ.get("GRAPH_VERSION", "v26.0")
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "30"))
FIELDS = ("id,message,created_time,permalink_url,full_picture,"
          "attachments{media_type,type,url,media,subattachments{media_type,media}}")


def http_get(url, binary=False, retries=3):
    last = None
    for k in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "uck-sync/1.0"}), timeout=30) as r:
                data = r.read()
                return data if binary else json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            # Les erreurs d'authentification ne se règlent pas en réessayant
            if e.code in (400, 401, 403):
                raise RuntimeError("HTTP %s : %s" % (e.code, body))
            last = RuntimeError("HTTP %s : %s" % (e.code, body))
        except Exception as e:  # réseau
            last = e
        time.sleep(2 * (k + 1))
    raise RuntimeError(str(last))


def safe_id(pid):
    return re.sub(r"[^A-Za-z0-9_]", "_", pid)


def download(url, name):
    """Télécharge une image dans media/ ; renvoie le chemin relatif ou None."""
    os.makedirs(MEDIA, exist_ok=True)
    path = os.path.join(MEDIA, name)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return "media/" + name
    try:
        data = http_get(url, binary=True)
    except Exception as e:
        print("  image ignorée (%s)" % e)
        return None
    if len(data) < 200 or len(data) > 8_000_000:
        return None
    with open(path, "wb") as f:
        f.write(data)
    return "media/" + name


def convert(post):
    pid = safe_id(post["id"])
    media, seen = [], set()
    atts = (post.get("attachments") or {}).get("data") or []

    def add_image(u, suffix):
        if not u or u in seen:
            return
        seen.add(u)
        local = download(u, "fb_%s_%s.jpg" % (pid, suffix))
        if local:
            media.append({"type": "image", "src": local})

    has_video = False
    for a in atts:
        subs = (a.get("subattachments") or {}).get("data") or []
        if subs:
            for k, s in enumerate(subs):
                img = ((s.get("media") or {}).get("image") or {}).get("src")
                add_image(img, "a%d" % k)
        elif a.get("media_type") == "video":
            has_video = True
        else:
            img = ((a.get("media") or {}).get("image") or {}).get("src")
            add_image(img, "a0")
    if not media:
        add_image(post.get("full_picture"), "p")
    if has_video and post.get("permalink_url"):
        media.insert(0, {"type": "facebook", "url": post["permalink_url"]})

    text = (post.get("message") or "").strip()
    first = text.split("\n", 1)[0].strip()
    title = first if 0 < len(first) <= 90 else ""
    body = text[len(first):].strip() if title else text
    return {
        "id": "fb_" + pid,
        "source": "facebook",
        "date": post.get("created_time", ""),
        "title": title,
        "text": body,
        "url": post.get("permalink_url", ""),
        "media": media,
    }


def main():
    page, token = os.environ.get("FB_PAGE_ID"), os.environ.get("FB_PAGE_TOKEN")
    if not page or not token:
        print("FB_PAGE_ID ou FB_PAGE_TOKEN absent : rien à faire (les secrets ne sont pas encore configurés).")
        return 0
    q = urllib.parse.urlencode({"fields": FIELDS, "limit": "15", "access_token": token})
    url = "%s/%s/%s/posts?%s" % (BASE, VERSION, urllib.parse.quote(page), q)
    try:
        data = http_get(url)
    except Exception as e:
        # On n'affiche jamais le jeton
        print("ÉCHEC de la lecture Facebook :", str(e).replace(token, "***"))
        return 1
    posts = data.get("data")
    if not isinstance(posts, list):
        print("Réponse inattendue :", str(data)[:200].replace(token, "***"))
        return 1

    try:
        old = json.load(open(NEWS, encoding="utf-8"))
        old_items = old.get("items", [])
    except Exception:
        old_items = []
    manual = [i for i in old_items if i.get("source") != "facebook"]
    fb = []
    for p in posts:
        if "id" not in p:
            continue
        item = convert(p)
        if item["title"] or item["text"] or item["media"]:
            fb.append(item)
    items = sorted(manual + fb, key=lambda i: i.get("date", ""), reverse=True)[:MAX_ITEMS]

    # supprime les images Facebook devenues inutiles
    used = {m["src"] for i in items for m in i.get("media", []) if m.get("type") == "image"}
    if os.path.isdir(MEDIA):
        for f in os.listdir(MEDIA):
            if f.startswith("fb_") and "media/" + f not in used:
                os.remove(os.path.join(MEDIA, f))

    out = {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "items": items}
    # N'écrit que si le contenu a changé (évite un commit par jour)
    prev_items = old_items
    if prev_items == items:
        print("Aucun changement (%d publications)." % len(items))
        return 0
    with open(NEWS, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("news.json mis à jour : %d publications (%d de Facebook)." % (len(items), len(fb)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

