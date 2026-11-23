"""
Checkd News-Aggregator
======================
Holt News aus den RSS-Quellen in sources.json, bringt sie in ein einheitliches Format,
entfernt doppelte Themen, bewertet die Wichtigkeit und schreibt public/feed.json.

Läuft stündlich als GitHub Action (siehe .github/workflows/feed.yml).
Nur Python-Standardbibliothek – keine Installation nötig.

Lokal testen:  python aggregator/build_feed.py
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import html
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "public"
USER_AGENT = "CheckdFeed/1.0 (+https://github.com/checkd-feed; Nachrichten-Uebersicht fuer eine private App)"
CATEGORIES = ["oesterreich", "welt", "wirtschaft", "tech", "sport"]


# ---------------------------------------------------------------- Laden

def fetch(url: str, timeout: int = 15) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:  # eine kaputte Quelle darf den Feed nicht verhindern
        print(f"  ! {url}: {e}", file=sys.stderr)
        return None


def local(tag: str) -> str:
    """'{namespace}title' -> 'title'"""
    return tag.rsplit("}", 1)[-1]


def child_text(el: ET.Element, *names: str) -> str:
    for c in el:
        if local(c.tag) in names and (c.text or "").strip():
            return c.text.strip()
    return ""


def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def teaser(s: str, max_len: int = 280) -> str:
    """Nur ein kurzer Teaser (keine ganzen Artikel kopieren). Schneidet an einem Satzende ab."""
    s = strip_html(s)
    s = re.sub(r"\[\s*mehr\s*\]|\[mehr\]|Weiterlesen.*$|mehr$", "", s, flags=re.I).strip()
    if len(s) <= max_len:
        return s
    cut = s[:max_len]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[: end + 1] if end > 80 else cut.rsplit(" ", 1)[0] + " …").strip()


def parse_date(s: str) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def find_image(item: ET.Element, raw_desc: str) -> str | None:
    best, best_w = None, -1
    for el in item.iter():
        name = local(el.tag)
        url = el.attrib.get("url")
        if name in ("content", "thumbnail") and url:
            w = int(el.attrib.get("width", "0") or 0)
            if w > best_w:
                best, best_w = url, w
        elif name == "enclosure" and url and el.attrib.get("type", "").startswith("image"):
            return url
    if best:
        return best
    for c in item:
        if local(c.tag) == "encoded" and c.text:
            m = re.search(r'<img[^>]+src="([^"]+)"', c.text)
            if m:
                return m.group(1)
    m = re.search(r'<img[^>]+src="([^"]+)"', raw_desc or "")
    return m.group(1) if m else None


def og_image(url: str) -> str | None:
    """Vorschaubild der Seite (für Quellen ohne Bild im RSS, z. B. Die Presse)."""
    data = fetch(url, timeout=8)
    if not data:
        return None
    head = data[:200_000].decode("utf-8", "ignore")
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', head) or \
        re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', head)
    return html.unescape(m.group(1)) if m else None


# ---------------------------------------------------------------- Kategorien

KEYWORDS = {
    "sport": ["fußball", "fussball", "tennis", "ski", "formel 1", "bundesliga", "champions league", "olymp",
              "rapid", "salzburg siegt", "nba", "nfl", "wm ", "em ", "trainer", "tor ", "match", "slalom", "abfahrt"],
    "tech": ["ki ", "künstliche intelligenz", "openai", "google", "apple", "microsoft", "nvidia", "chip", "software",
             "app ", "smartphone", "internet", "cyber", "hacker", "daten", "tech", "elon musk", "tesla", "meta "],
    "wirtschaft": ["wirtschaft", "inflation", "zinsen", "ezb", "börse", "aktie", "konzern", "unternehmen", "budget",
                   "steuer", "pleite", "insolvenz", "export", "arbeitsmarkt", "arbeitslos", "fusion", "preise", "milliarden"],
    "oesterreich": ["österreich", "wien", "graz", "linz", "salzburg", "innsbruck", "kärnten", "tirol", "vorarlberg",
                    "niederösterreich", "oberösterreich", "steiermark", "burgenland", "övp", "spö", "fpö", "neos",
                    "grüne", "nationalrat", "bundesregierung", "kanzler", "van der bellen", "bundesheer"],
}

def has_word(text: str, word: str) -> bool:
    """Stichwort als Wortanfang suchen ("nba" darf nicht in "offenbar" treffen).
    Kurze Wörter (<= 4 Buchstaben) müssen ganz passen."""
    w = re.escape(word.strip())
    end = r"(?![a-zäöüß])" if len(word.strip()) <= 4 else ""
    return re.search(rf"(?<![a-zäöüß]){w}{end}", text) is not None


SUBJECT_MAP = {  # RSS-Kategorien/Rubriken -> unsere Kategorien
    "inland": "oesterreich", "chronik": "oesterreich", "politik": "oesterreich", "österreich": "oesterreich",
    "wien": "oesterreich", "ausland": "welt", "international": "welt", "welt": "welt",
    "wirtschaft": "wirtschaft", "economy": "wirtschaft", "business": "wirtschaft",
    "sport": "sport", "web": "tech", "it": "tech", "netzpolitik": "tech", "technologie": "tech", "digital": "tech",
    "science": "tech", "wissenschaft": "tech", "tech": "tech",
}


# Wortanfänge (Stämme): "russ" trifft Russland, russisch, Russen …
FOREIGN = ["deutsch", "berlin", "bayern", "usa", "us-", "amerika", "trump", "washington", "chin", "russ", "moskau",
           "kreml", "putin", "ukrain", "kiew", "selenskyj", "frankreich", "franzö", "itali", "spani", "schweiz",
           "britisch", "großbritannien", "london", "paris", "israel", "gaza", "iran", "syri", "liby", "eu-", "nato",
           "brüssel", "japan", "indi", "türk", "polen", "polnisch", "ungarn", "ungarisch", "afrika", "nahost"]


def austria_check(cat: str, title: str, desc: str) -> str:
    if cat == "unklar":
        return cat
    """'Österreich' nur, wenn es wirklich um Österreich geht – sonst 'Welt'."""
    if cat != "oesterreich":
        return cat
    text = f" {title} {desc} ".lower()
    at = any(has_word(text, w) for w in KEYWORDS["oesterreich"])
    foreign = any(has_word(text, w) for w in FOREIGN)
    return "welt" if foreign and not at else cat


def classify(source: dict, title: str, desc: str, link: str, subject: str) -> str:
    return austria_check(_classify(source, title, desc, link, subject), title, desc)


def _classify(source: dict, title: str, desc: str, link: str, subject: str) -> str:
    if source["category"] != "auto":
        return source["category"]
    for key in [subject.lower(), link.lower()]:
        parts = set(re.findall(r"[a-zäöü]+", key))
        for k, cat in SUBJECT_MAP.items():
            if k in parts:
                # Tagesschau "Inland" = Deutschland -> für uns "welt"
                if cat == "oesterreich" and source["id"] == "tagesschau":
                    return "welt"
                return cat
    text = f" {title} {desc} ".lower()
    scores = {c: sum(1 for w in words if has_word(text, w)) for c, words in KEYWORDS.items()}
    best = max(scores, key=scores.get)
    if scores[best] > 0:
        return best
    if any(has_word(text, w) for w in FOREIGN):
        return "welt"
    return "unklar"   # wird später nur behalten, wenn mehrere Quellen berichten


# ---------------------------------------------------------------- Parsen

def parse_feed(source: dict) -> list[dict]:
    data = fetch(source["url"])
    if not data:
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        print(f"  ! {source['id']}: XML-Fehler {e}", file=sys.stderr)
        return []
    items = [el for el in root.iter() if local(el.tag) in ("item", "entry")]
    out = []
    for it in items:
        title = strip_html(child_text(it, "title"))
        link = child_text(it, "link") or (it.find("{*}link").attrib.get("href", "") if it.find("{*}link") is not None else "")
        raw_desc = child_text(it, "description", "summary")
        date = parse_date(child_text(it, "pubDate", "date", "published", "updated"))
        subject = " ".join(c.text or "" for c in it if local(c.tag) in ("subject", "category"))
        if not title or not link:
            continue
        summary = teaser(raw_desc)
        if "[premium]" in title.lower() or "premium" in subject.lower():
            continue          # Bezahl-Artikel kann man ohne Abo nicht lesen
        if len(summary) < 40 or summary.lower().rstrip(". ") == title.lower().rstrip(". "):
            continue          # nur Titel ohne Teaser (z. B. Video-Hinweise)
        out.append({
            "title": title,
            "summary": summary,
            "source": source["name"],
            "sourceId": source["id"],
            "sourceUrl": link.strip(),
            "imageUrl": find_image(it, raw_desc),
            "category": classify(source, title, strip_html(raw_desc), link, subject),
            "publishedAt": date,
            "weight": source["weight"],
            "lang": source.get("lang", "de"),
        })
    print(f"  {source['id']:<11} {len(out):>3} Meldungen")
    return out


# ---------------------------------------------------------------- Duplikate

STOP = set("""der die das und oder aber ein eine einer eines einem einen nicht mit von für fur auf aus bei nach
über uber unter vor zum zur den dem des im in am an als auch ist sind war wird werden hat haben sich noch nun
wie was wer wo so es er sie wir ihr man mehr neue neuer neues jetzt heute gegen bis seit durch""".split())


def tokens(title: str) -> set[str]:
    t = unicodedata.normalize("NFKD", title.lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    words = re.findall(r"[a-z0-9]{4,}", t)
    return {w[:6] for w in words if w not in STOP}   # grobe "Stammform": erste 6 Buchstaben


def similar(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return False
    inter = len(a & b)
    return inter >= 3 or (inter >= 2 and inter / min(len(a), len(b)) >= 0.5)


def dedupe(items: list[dict]) -> list[dict]:
    """Gleiches Thema aus mehreren Quellen -> ein Eintrag; merkt sich, wie viele Quellen berichten."""
    groups: list[dict] = []
    for it in sorted(items, key=lambda x: (-x["weight"], x["publishedAt"] or datetime.min.replace(tzinfo=timezone.utc))):
        tk = tokens(it["title"])
        for g in groups:
            if g["lang"] == it["lang"] and similar(tk, g["_tokens"]):
                g["_sources"].add(it["source"])
                # Bild NICHT von einer anderen Quelle übernehmen – es gehört zu deren Artikel
                break
        else:
            groups.append({**it, "_tokens": tk, "_sources": {it["source"]}})
    return groups


# ---------------------------------------------------------------- Wichtigkeit (Version 1: Regeln)

SOFT = ["wetter", "rezept", "quiz", "horoskop", "gewinnspiel", "podcast", "kommentar", "kolumne", "liveticker",
        "newsblog", "live:", "bildergalerie", "tipps", "so geht", "test:", "rätsel", "promi", "royals"]


def importance(g: dict, now: datetime) -> int:
    score = g["weight"]                                   # Quelle (20-32)
    score += min(45, 15 * (len(g["_sources"]) - 1))       # mehrere Quellen berichten -> wichtiger
    age_h = (now - g["publishedAt"]).total_seconds() / 3600 if g["publishedAt"] else 24
    score += 20 if age_h < 3 else 12 if age_h < 8 else 5 if age_h < 16 else 0
    if g["category"] in ("oesterreich", "welt"):
        score += 5
    if any(has_word(g["title"].lower(), w) for w in SOFT):
        score -= 25
    if g["title"].rstrip().endswith("?") and len(g["_sources"]) == 1:
        score -= 10       # Fragen-Titel sind oft Umfragen/Meinung, keine harte News
    return max(0, min(100, int(score)))


# ---------------------------------------------------------------- YouTube Shorts (Phase 4)
# Nur Metadaten (Titel, Kanal, Video-ID, Vorschaubild). Abgespielt wird in der App
# ausschließlich über den offiziellen YouTube-Embed-Player – nichts wird heruntergeladen.
# Der API-Key kommt aus dem GitHub-Secret YOUTUBE_API_KEY und steht nie im Code oder in der App.

YT_API = "https://www.googleapis.com/youtube/v3/"


def yt_get(endpoint: str, key: str, **params) -> dict | None:
    q = urllib.parse.urlencode({**params, "key": key})
    try:
        req = urllib.request.Request(YT_API + endpoint + "?" + q, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:   # nie die URL ausgeben (enthält den Key)
        print(f"  ! YouTube {endpoint}: HTTP {e.code}", file=sys.stderr)
    except Exception as e:
        print(f"  ! YouTube {endpoint}: {type(e).__name__}", file=sys.stderr)
    return None


def iso_duration(s: str) -> int:
    """'PT1M5S' -> 65 Sekunden"""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def youtube_channel(ch: dict, key: str, now: datetime, max_age_h: int, max_sec: int) -> list[dict]:
    info = yt_get("channels", key, part="snippet,contentDetails", forHandle=ch["handle"])
    if not info or not info.get("items"):
        print(f"  ! YouTube-Kanal nicht gefunden: {ch['handle']}", file=sys.stderr)
        return []
    c = info["items"][0]
    uploads = c["contentDetails"]["relatedPlaylists"]["uploads"]
    name = ch.get("name") or c["snippet"]["title"]
    pl = yt_get("playlistItems", key, part="contentDetails", playlistId=uploads, maxResults=25)
    ids = [i["contentDetails"]["videoId"] for i in (pl or {}).get("items", [])]
    if not ids:
        return []
    vids = yt_get("videos", key, part="snippet,contentDetails,status", id=",".join(ids))
    out = []
    for v in (vids or {}).get("items", []):
        sn, cd, stt = v["snippet"], v["contentDetails"], v.get("status", {})
        dur = iso_duration(cd.get("duration"))
        published = parse_date(sn.get("publishedAt", ""))
        if not (0 < dur <= max_sec) or not stt.get("embeddable", False):
            continue
        if sn.get("liveBroadcastContent", "none") != "none" or not published or now - published > timedelta(hours=max_age_h):
            continue
        rr = cd.get("regionRestriction", {})
        if "AT" in rr.get("blocked", []) or ("allowed" in rr and "AT" not in rr["allowed"]):
            continue
        title = html.unescape(sn.get("title", "")).strip()
        desc = teaser(sn.get("description", ""), 200)
        thumbs = sn.get("thumbnails", {})
        thumb = next((thumbs[k]["url"] for k in ("maxres", "standard", "high", "medium", "default") if k in thumbs), None)
        cat = classify({"id": ch["id"], "category": ch.get("category", "auto")}, title, desc, "", "")
        if cat == "unklar":
            cat = ch.get("fallback", "welt")
        age_h = (now - published).total_seconds() / 3600
        out.append({
            "id": "yt-" + v["id"],
            "type": "video",
            "platform": "youtube",
            "videoId": v["id"],
            "title": title,
            "summary": "",
            "source": name,
            "sourceUrl": f"https://www.youtube.com/shorts/{v['id']}",
            "imageUrl": thumb,
            "category": cat,
            "importance": max(0, min(100, int(ch.get("weight", 20) + (20 if age_h < 6 else 10 if age_h < 24 else 0)))),
            "coveredBy": [name],
            "lang": ch.get("lang", "de"),
            "durationSec": dur,
            "publishedAt": published.isoformat().replace("+00:00", "Z"),
        })
    print(f"  YouTube {ch['handle']}: {len(out)} Shorts")
    return out


def youtube_items(cfg: dict, now: datetime) -> list[dict]:
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    channels = cfg.get("youtube", [])
    if not key or not channels:
        print("YouTube: übersprungen (kein Key oder keine Kanäle)")
        return []
    st = cfg["settings"]
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        vids = [v for lst in ex.map(lambda ch: youtube_channel(
            ch, key, now, st.get("videoMaxAgeHours", 48), st.get("videoMaxSeconds", 180)), channels) for v in lst]
    # Pro Kanal höchstens 4, insgesamt die wichtigsten/neuesten
    per, out = {}, []
    for v in sorted(vids, key=lambda v: (v["importance"], v["publishedAt"]), reverse=True):
        if per.get(v["source"], 0) < 4:
            out.append(v)
            per[v["source"]] = per.get(v["source"], 0) + 1
    return out[: st.get("maxVideos", 20)]


# ---------------------------------------------------------------- Hauptprogramm

def main() -> None:
    cfg = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    st = cfg["settings"]
    now = datetime.now(timezone.utc)
    print("Lade Quellen …")
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        raw = [x for lst in ex.map(parse_feed, cfg["rss"]) for x in lst]

    fresh = [x for x in raw if x["publishedAt"] and now - x["publishedAt"] < timedelta(hours=st["maxAgeHours"])]
    groups = dedupe(fresh)
    for g in groups:
        if g["category"] == "unklar":   # Thema ohne erkennbare Kategorie: nur wenn mehrere Quellen berichten
            g["category"] = "welt" if len(g["_sources"]) >= 2 else None
    groups = [g for g in groups if g["category"]]
    for g in groups:
        g["importance"] = importance(g, now)

    # Pro Kategorie die wichtigsten, damit jeder Filter in der App genug hat
    picked = []
    for cat in CATEGORIES:
        in_cat = sorted((g for g in groups if g["category"] == cat), key=lambda g: -g["importance"])
        picked += in_cat[: st["perCategory"]]
    picked = sorted(picked, key=lambda g: -g["importance"])[: st["maxItems"]]

    # Fehlende Bilder: Vorschaubild der Seite holen (nur für die ausgewählten Meldungen)
    missing = [g for g in picked if not g.get("imageUrl")]
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for g, img in zip(missing, ex.map(lambda g: og_image(g["sourceUrl"]), missing)):
            g["imageUrl"] = img

    items = [{
        "id": hashlib.sha1(g["sourceUrl"].encode()).hexdigest()[:16],
        "type": "article",
        "title": g["title"],
        "summary": g["summary"],
        "source": g["source"],
        "sourceUrl": g["sourceUrl"],
        "imageUrl": g.get("imageUrl"),
        "category": g["category"],
        "importance": g["importance"],
        "coveredBy": sorted(g["_sources"]),
        "lang": g["lang"],
        "publishedAt": g["publishedAt"].isoformat().replace("+00:00", "Z"),
    } for g in picked]

    videos = youtube_items(cfg, now)
    items += videos

    OUT_DIR.mkdir(exist_ok=True)
    feed = {"version": 1, "generatedAt": now.isoformat().replace("+00:00", "Z"), "items": items}
    (OUT_DIR / "feed.json").write_text(json.dumps(feed, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT_DIR / "index.html").write_text(
        "<!doctype html><meta charset=utf-8><title>Checkd Feed</title>"
        f"<p>Checkd News-Feed · {len(items)} Meldungen · {feed['generatedAt']}</p><a href=feed.json>feed.json</a>",
        encoding="utf-8")
    counts = {c: sum(1 for i in items if i["category"] == c) for c in CATEGORIES}
    print(f"Videos: {len(videos)}")
    print(f"{len(raw)} geladen, {len(fresh)} aktuell, {len(groups)} Themen -> {len(items)} im Feed {counts}")


if __name__ == "__main__":
    main()
