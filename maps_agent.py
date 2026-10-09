"""Agent (OpenAI ou Gemini) + Playwright MCP : récupère les premiers résultats Google Maps.

Usage :
    .venv\\Scripts\\python.exe maps_agent.py "usine agroalimentaire Aïn Sebaâ Casablanca"

L'extraction (scroll + clic sur chaque fiche) se fait dans le navigateur en un seul
appel d'outil : l'agent n'a besoin que de ~2 requêtes Gemini par recherche.
"""
import asyncio
import csv
import json
import logging
import os
import shutil
import sys
import warnings
from urllib.parse import quote_plus

from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

load_dotenv()

# Bruit inoffensif : Gemini ignore "$schema"/"additionalProperties" des outils MCP,
# et create_react_agent est juste marqué déprécié (toujours fonctionnel en LangGraph 1.x).
logging.getLogger("langchain_google_genai._function_utils").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", message=".*create_react_agent has been moved.*")

MAX_RESULTS = int(os.getenv("MAX_RESULTS", "2"))
FIELDS = ["nom", "categorie", "adresse", "telephone", "note", "nb_avis", "horaires", "site_web", "lien_maps"]

# Exécuté dans la page Google Maps : charge la liste, ouvre chaque fiche et lit ses champs.
EXTRACT_JS = r"""async () => {
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
    if (document.querySelector('h1.DUwDvf')) return { error: 'single_result' };
    return { error: 'no_results_list' };
  }

  const cards = () => [...feed.querySelectorAll('a[href*="/maps/place/"]')]
    .filter(a => !/Sponsoris|Sponsored/i.test(a.parentElement.innerText));
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
    categorie: q('button.DkEaL')?.textContent.trim() || '',
    adresse: label('[data-item-id="address"]', /^Adresse\s*:\s*/),
    telephone: label('[data-item-id^="phone:tel:"]', /^Numéro de téléphone\s*:\s*/),
    note: (imgs.find(t => /étoiles\s*$/.test(t)) || '').replace(/\s*étoiles\s*$/, ''),
    nb_avis: (imgs.find(t => /^[\d\s\u202f.]+avis$/.test(t)) || '').replace(/\s*avis$/, ''),
    horaires: hoursRows.length ? hoursRows.join(' | ')
      : (q('[aria-label*="Copier les horaires"]')?.getAttribute('aria-label') || '').replace(/,\s*Copier.*$/, ''),
    site_web: q('a[data-item-id="authority"]')?.href || '',
  };
}"""

SYSTEM = """Tu aides à trouver des établissements sur Google Maps.
Appelle l'outil rechercher_google_maps UNE SEULE FOIS avec une requête précise
(type d'établissement + quartier + ville). Ensuite, réponds en français avec un
court résumé (nombre de résultats, remarques utiles, ex. résultats hors sujet).
Ne recopie pas toutes les fiches : elles sont déjà enregistrées en CSV."""


def text_of(content) -> str:
    """Gemini et MCP peuvent renvoyer une liste de blocs au lieu d'une chaîne."""
    if isinstance(content, list):
        return "".join(
            b.get("text", "") if isinstance(b, dict) else getattr(b, "text", str(b)) for b in content
        )
    return str(content)


def parse_evaluate(text: str):
    """browser_evaluate renvoie '### Result\\n<json>\\n### Ran Playwright code...'."""
    body = text.split("### Result", 1)[-1].split("\n### ", 1)[0]
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"error": text[:1000]}


async def extraire(session, requete: str) -> list[dict] | dict:
    url = f"https://www.google.com/maps/search/{quote_plus(requete)}?hl=fr"
    print(f"   🌐 browser_navigate {url}")
    await session.call_tool("browser_navigate", {"url": url})
    print(f"   📜 browser_evaluate : scroll de la liste jusqu'à {MAX_RESULTS} résultats…")
    res = await session.call_tool("browser_evaluate", {"function": EXTRACT_JS})
    cards = parse_evaluate(text_of(res.content))
    if isinstance(cards, dict):
        return cards

    rows = []
    for i, card in enumerate(cards, 1):
        print(f"   📄 [{i}/{len(cards)}] {card['nom']}")
        await session.call_tool("browser_navigate", {"url": card["lien_maps"]})
        detail = parse_evaluate(text_of((await session.call_tool("browser_evaluate", {"function": DETAIL_JS})).content))
        if "error" in detail:
            print(f"      ⚠ fiche non lue : {detail['error'][:120]}")
            detail = {}
        rows.append({**card, **{k: v for k, v in detail.items() if v}, "lien_maps": card["lien_maps"].split("?")[0]})
    return rows


def save_csv(rows: list[dict], path: str = "resultats.csv") -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def print_rows(rows: list[dict]) -> None:
    for i, r in enumerate(rows, 1):
        print(f"\n{i}. {r.get('nom', '')}")
        for k in FIELDS[1:]:
            if r.get(k):
                print(f"   {k}: {r[k]}")


def make_llm():
    """OpenAI si OPENAI_API_KEY est défini dans .env, sinon Gemini."""
    if os.getenv("OPENAI_API_KEY"):
        return ChatOpenAI(model=os.getenv("OPENAI_MODEL", "gpt-5-mini"))
    return ChatGoogleGenerativeAI(model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"), temperature=0)


async def main(query: str) -> None:
    client = MultiServerMCPClient({
        "playwright": {
            "command": shutil.which("npx") or "npx",  # npx.cmd sous Windows
            "args": ["@playwright/mcp@latest", "--isolated"],
            "transport": "stdio",
        }
    })
    # Une seule session = un seul navigateur pour toute la recherche.
    # (client.get_tools() relance un navigateur neuf à chaque appel d'outil.)
    async with client.session("playwright") as session:
        results: list[dict] = []

        @tool
        async def rechercher_google_maps(requete: str) -> str:
            """Cherche `requete` sur Google Maps et renvoie les premières fiches
            (nom, catégorie, adresse, téléphone, note, avis, horaires, site web)."""
            data = await extraire(session, requete)
            if isinstance(data, dict):  # erreur côté page
                return json.dumps(data, ensure_ascii=False)
            results[:] = data
            return json.dumps(data, ensure_ascii=False)

        llm = make_llm()
        agent = create_react_agent(llm, [rechercher_google_maps], prompt=SYSTEM)

        print(f"▶ Agent lancé ({llm.__class__.__name__}) — recherche : {query}")
        summary = ""
        async for update in agent.astream(
            {"messages": [("user", f"Trouve les {MAX_RESULTS} premiers résultats Google Maps pour : {query}")]},
            config={"recursion_limit": 6},
            stream_mode="updates",
        ):
            for node, data in update.items():
                for msg in data.get("messages", []):
                    if node == "agent" and getattr(msg, "tool_calls", None):
                        for call in msg.tool_calls:
                            print(f"\n🤖 {call['name']}  {json.dumps(call['args'], ensure_ascii=False)}")
                    elif node == "tools":
                        print(f"   ↳ {len(results)} fiches récupérées")
                    elif node == "agent":
                        summary = text_of(msg.content)

    print_rows(results)
    if results:
        save_csv(results)
        print(f"\n{len(results)} résultats enregistrés dans resultats.csv")
    print(f"\n💬 {summary}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # emojis/accents dans la console Windows
    q = " ".join(sys.argv[1:]) or input("Recherche Google Maps : ")
    asyncio.run(main(q))
