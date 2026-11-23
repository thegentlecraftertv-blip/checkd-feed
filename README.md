# checkd-feed

News-Feed für die Checkd-App. Ein GitHub-Action-Job baut stündlich `feed.json`
aus den Quellen in `sources.json` und veröffentlicht sie auf GitHub Pages.

- **Quellen ändern:** `sources.json` bearbeiten → nach dem Speichern wird der Feed neu gebaut
- **Sofort neu bauen:** Tab *Actions* → *News-Feed bauen* → *Run workflow*
- **Lokal testen:** `python aggregator/build_feed.py` → Ergebnis in `public/feed.json`

Nur Titel, kurzer Teaser, Bild-Link und Link zum Original – keine kompletten Artikel.
Videos (YouTube Shorts) ausschließlich über den offiziellen YouTube-Embed-Player.
