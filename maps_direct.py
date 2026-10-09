"""Extraction Google Maps directe, sans LLM (0 token, pas de clé API).
Script autonome : ne dépend pas de maps_agent.py.

Usage :
    .venv\\Scripts\\python.exe maps_direct.py "usine agroalimentaire Aïn Sebaâ Casablanca"
Nombre de fiches : MAX_RESULTS dans .env (2 par défaut).
"""
import asyncio
import csv
import json
import os
import re
import shutil
import sys
from urllib.parse import quote_plus, unquote

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

MAX_RESULTS = int(os.getenv("MAX_RESULTS", "2"))
FIELDS = ["nom", "categorie", "adresse", "telephone", "note", "nb_avis", "horaires", "site_web",
          "latitude", "longitude", "place_id", "cid", "feature_id", "lien_maps"]

# Exécuté dans la page de recherche : scroll de la liste puis renvoie nom + lien des MAX premières fiches.
LIST_JS = r"""async () => {
  const MAX = __MAX__;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const waitFor = async (fn, ms = 10000) => {
    for (let t = 0; t < ms; t += 300) { const v = fn(); if (v) return v; await sleep(300); }
    return null;
  };

  // Bannière de consentement éventuelle
  const reject = [...document.querySelectorAll('button')].find(b => /Tout refuser|Reject all/i.test(b.textContent));
  if (reject) { reject.click(); await sleep(2000); }

  const feed = await waitFor(() => document.querySelector('[role=feed]'), 15000);
  if (!feed) {
    // Un seul résultat : Google ouvre directement la fiche
    if (document.querySelector('[role=main] h1')) return { error: 'single_result' };
    return { error: 'no_results_list' };
  }

  // Fiches non sponsorisées, sans doublon (Google répète parfois un lieu plus bas dans la liste)
  const cards = () => {
    const seen = new Set();
    return [...feed.querySelectorAll('a[href*="/maps/place/"]')]
      .filter(a => !/Sponsoris|Sponsored/i.test(a.parentElement.innerText))
      .filter(a => {
        const key = (a.href.match(/!1s(0x[0-9a-f]+:0x[0-9a-f]+)/) || [a.href.split('?')[0]])[0];
        return seen.has(key) ? false : seen.add(key);
      });
  };
  // Scroll du panneau gauche jusqu'à MAX fiches, ou 3 scrolls d'affilée sans nouveau résultat
  for (let i = 0, stale = 0; i < 15 && stale < 3 && cards().length < MAX; i++) {
    const before = cards().length;
    feed.scrollTo(0, feed.scrollHeight);
    await waitFor(() => cards().length > before, 3000);
    await sleep(800);
    stale = cards().length > before ? 0 : stale + 1;
    if (/Vous êtes arrivé à la fin|reached the end/i.test(feed.innerText)) break;
  }

  return cards().slice(0, MAX).map(a => ({ nom: a.getAttribute('aria-label') || '', lien_maps: a.href }));
}""".replace("__MAX__", str(MAX_RESULTS))

# Exécuté sur la page d'une fiche : attend son chargement puis lit les champs.
DETAIL_JS = r"""async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const waitFor = async (fn, ms) => {
    for (let t = 0; t < ms; t += 300) { const v = fn(); if (v) return v; await sleep(300); }
    return null;
  };
  const h1 = await waitFor(() => document.querySelector('[role=main] h1'), 15000);
  const r = h1?.closest('[role=main]') || document;
  // Laisse le temps aux blocs adresse/téléphone/horaires de s'afficher
  await waitFor(() => r.querySelector('[data-item-id]'), 6000);
  await sleep(1000);
  const q = s => r.querySelector(s);
  const label = (s, prefix) => (q(s)?.getAttribute('aria-label') || '').replace(prefix, '').trim();
  const imgs = [...r.querySelectorAll('[role=img][aria-label]')].map(e => e.getAttribute('aria-label'));
  const hoursRows = [...r.querySelectorAll('table tr')]
    .map(tr => [...tr.querySelectorAll('td')].map(td => td.innerText.trim()).filter(Boolean).join(' '))
    .filter(t => /^(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)/i.test(t));
  return {
    nom: h1?.textContent.trim() || '',
    // Sélecteurs basés sur data-item-id / aria-label / jsaction (plus stables que les classes CSS générées)
    categorie: (q('button[jsaction$=".category"]') || q('button.DkEaL'))?.textContent.trim() || '',
    adresse: label('[data-item-id="address"]', /^Adresse\s*:\s*/),
    telephone: label('[data-item-id^="phone:tel:"]', /^Numéro de téléphone\s*:\s*/),
    note: (imgs.find(t => /étoiles\s*$/.test(t)) || '').replace(/\s*étoiles\s*$/, ''),
    nb_avis: (imgs.find(t => /^[\d\s .]+avis$/.test(t)) || '').replace(/\s*avis$/, ''),
    horaires: hoursRows.length ? hoursRows.join(' | ')
      : (q('[aria-label*="Copier les horaires"]')?.getAttribute('aria-label') || '').replace(/,\s*Copier.*$/, ''),
    site_web: q('a[data-item-id="authority"]')?.href || '',
  };
}"""


def parse_maps_link(url: str) -> dict:
    """Extrait coordonnées et identifiants d'un lien /maps/place/.../data=...
    !3d<lat> !4d<lng> · !19s<place_id ChIJ…> · !1s0x<a>:0x<b> (feature id, CID = int(b))."""
    def grab(pattern: str) -> str:
        m = re.search(pattern, url)
        return m.group(1) if m else ""

    feature_id = grab(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)")
    return {
        "latitude": grab(r"!3d(-?\d+(?:\.\d+)?)"),
        "longitude": grab(r"!4d(-?\d+(?:\.\d+)?)"),
        "place_id": unquote(grab(r"!19s([^!?]+)")),
        "cid": str(int(feature_id.split(":")[1], 16)) if feature_id else "",
        "feature_id": feature_id,
    }


async def call(session: ClientSession, tool: str, args: dict):
    """Appelle un outil Playwright MCP ; pour browser_evaluate, renvoie le JSON du résultat."""
    res = await session.call_tool(tool, args)
    text = "".join(getattr(c, "text", "") for c in res.content)
    if tool != "browser_evaluate":
        return text
    # Format : '### Result\n<json>\n### Ran Playwright code...'
    body = text.split("### Result", 1)[-1].split("\n### ", 1)[0]
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"error": text[:1000]}


async def extraire(session: ClientSession, requete: str) -> list[dict] | dict:
    url = f"https://www.google.com/maps/search/{quote_plus(requete)}?hl=fr"
    print(f"   🌐 {url}")
    await call(session, "browser_navigate", {"url": url})
    print(f"   📜 scroll de la liste jusqu'à {MAX_RESULTS} résultats…")
    cards = await call(session, "browser_evaluate", {"function": LIST_JS})
    if isinstance(cards, dict):
        return cards

    rows = []
    for i, card in enumerate(cards, 1):
        print(f"   📄 [{i}/{len(cards)}] {card['nom']}")
        await call(session, "browser_navigate", {"url": card["lien_maps"]})
        detail = await call(session, "browser_evaluate", {"function": DETAIL_JS})
        if "error" in detail:
            print(f"      ⚠ fiche non lue : {detail['error'][:120]}")
            detail = {}
        link = card["lien_maps"].split("?")[0]
        rows.append({**card, **{k: v for k, v in detail.items() if v}, **parse_maps_link(link), "lien_maps": link})
    return rows


async def main(query: str) -> None:
    server = StdioServerParameters(
        command=shutil.which("npx") or "npx",  # npx.cmd sous Windows
        args=["@playwright/mcp@latest", "--isolated"],
    )
    # Une seule session = un seul navigateur pour toute la recherche
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        rows = await extraire(session, query)

    if isinstance(rows, dict):
        print(f"Erreur : {rows.get('error')}")
        return

    # Si un champ est vide pour TOUTES les fiches, Google a probablement changé sa page
    empty = [k for k in ("categorie", "adresse", "telephone", "note") if rows and not any(r.get(k) for r in rows)]
    if empty:
        print(f"\n⚠ Champs vides sur toutes les fiches : {', '.join(empty)} — les sélecteurs sont peut-être à mettre à jour.")

    for i, r in enumerate(rows, 1):
        print(f"\n{i}. {r.get('nom', '')}")
        for k in FIELDS[1:]:
            if r.get(k):
                print(f"   {k}: {r[k]}")

    with open("resultats.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n{len(rows)} résultats enregistrés dans resultats.csv")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # emojis/accents dans la console Windows
    q = " ".join(sys.argv[1:]) or input("Recherche Google Maps : ")
    asyncio.run(main(q))
