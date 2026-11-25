# SEC EDGAR MCP

En egen MCP-server som gir Claude direkte tilgang til SEC EDGAR, altså originalkilden for regnskapstall,
innsidehandel og selskapsdokumenter for amerikanske børsnoterte selskaper. Gratis data, ingen API-nøkkel.

Laget for å lete etter små, raskt voksende og lønnsomme selskaper, og deretter prøve å knekke hver kandidat
ved å lese dokumentene.

## Verktøy

| Verktøy | Hva det gjør |
|---|---|
| `screen_companies` | Screener **alle** SEC-registrerte selskaper samtidig på vekst, lønnsomhet og størrelse (XBRL frames) |
| `get_financials` | Års- og kvartalstall for ett selskap: omsetning, vekst, marginer, EPS, kontantstrøm, balanse |
| `get_insider_trades` | Innsidekjøp og -salg fra Form 4, med totaler, 10b5-1-flagg og fjerning av duplikater |
| `get_filings` | Liste over siste dokumenter (10-K, 10-Q, 8-K …) med 8-K-koder og lenker |
| `read_filing` | Les et dokument eller én seksjon som ren tekst: risikofaktorer, MD&A, revisorberetning, rettssaker, pressemeldinger |
| `search_filings` | Fulltekstsøk i alle dokumenter siden 2001, f.eks. `"record backlog"` eller `"material weakness"` |
| `lookup_company` | Finn ticker, navn og CIK |

Det SEC **ikke** har: aksjekurser. P/E og markedsverdi må regnes ut med en kurs fra et annet sted
(`get_financials` gir antall aksjer).

## Arkitektur

```
claude.ai  ──HTTPS + Bearer-token──>  denne serveren (Render, gratis)  ──>  data.sec.gov / www.sec.gov / efts.sec.gov
                                        • maks 8 kall/sek (SEC tillater 10)
                                        • minnecache med størrelsesgrense
```

```
src/sec_edgar_mcp/
  server.py       MCP-verktøyene, token-sjekk, HTTP-app
  sec_client.py   HTTP mot SEC: User-Agent, rate limit, retry, cache
  screener.py     screening via XBRL frames
  financials.py   companyfacts → rene års-/kvartalstall (håndterer ulike XBRL-tagger, avleder Q4)
  insiders.py     Form 4-parsing
  filings.py      dokumentlister, HTML → tekst, seksjoner, fulltekstsøk
  companies.py    ticker/navn/CIK-oppslag
tests/            tester mot en falsk SEC (kjører uten nett)
scripts/          smoke_test.py: live-test mot ekte SEC
```

## 1. Kjør lokalt og test mot ekte SEC (10 min)

Krever Python 3.11+.

```bash
git clone https://github.com/<ditt-brukernavn>/sec-edgar-mcp.git
cd sec-edgar-mcp
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

export SEC_USER_AGENT="Ditt Navn din@epost.no"     # Windows PowerShell: $env:SEC_USER_AGENT="Ditt Navn din@epost.no"
pytest -q                          # tester uten nett
python scripts/smoke_test.py SMCI  # live mot SEC: hvert verktøy skal vise OK
```

SEC krever at User-Agent inneholder navn og e-post. Uten dette blir du blokkert.

## 2. Publiser på Render (gratis, ca. 15 min)

1. Lag en konto på [render.com](https://render.com) og koble til GitHub.
2. **New → Blueprint**, velg dette repoet. Render leser `render.yaml`.
3. Fyll inn `SEC_USER_AGENT` når du blir spurt. `MCP_AUTH_TOKEN` lages automatisk.
4. Når den er bygget: åpne `https://<navn>.onrender.com/health`. Den skal svare `{"status": "ok"}`.
5. Kopier `MCP_AUTH_TOKEN` fra **Environment**-fanen i Render.

Gratisnivået sovner etter ca. 15 minutter uten bruk. Første kall etter en pause kan ta 30–60 sekunder;
prøv igjen hvis Claude melder tidsavbrudd.

## 3. Koble til Claude

På [claude.ai/customize/connectors](https://claude.ai/customize/connectors): **+ Add → Add custom connector**

- **Name:** `SEC EDGAR`
- **URL:** `https://<navn>.onrender.com/mcp`
- **Authentication:** No sign in
- **Request headers:** `Authorization` = `Bearer <MCP_AUTH_TOKEN>`

Slå den på i en chat via **+ → Connectors**.

### Alternativ: lokalt i Claude Desktop (uten hosting)

I `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "sec-edgar": {
      "command": "/full/sti/til/sec-edgar-mcp/.venv/bin/sec-edgar-mcp",
      "args": ["stdio"],
      "env": { "SEC_USER_AGENT": "Ditt Navn din@epost.no" }
    }
  }
}
```

Merk: lokale servere virker bare i Claude Desktop-appen selv, ikke i claude.ai i nettleseren.

## Eksempler på spørsmål til Claude

- «Screen etter selskaper med over 40 % omsetningsvekst siste kvartal, lønnsomme, under 1 mrd. dollar i kvartalsomsetning. Sorter på vekst.»
- «Ta de fem øverste: hent finansielle trender, innsidehandel siste 180 dager og revisorberetningen. Flagg alt som ser rart ut.»
- «Søk i 8-K siste 60 dager etter "record backlog" og vis hvilke selskaper som nevner det oftest.»

## Begrensninger

- Bare US-GAAP-tall. Utenlandske selskaper som rapporterer i IFRS (20-F) dekkes dårlig av screening og `get_financials`.
- XBRL-data er selskapenes egne tagger. Tallene er gode, men uvanlige tagger kan gi hull. Sjekk mot dokumentet (`source_accession`).
- Seksjonsuttrekk er basert på overskrifter. Hvis en seksjon ikke blir funnet, les `section="full"` med `offset`.
- Ingen kurser, analytikerestimater eller transkripsjoner av resultatpresentasjoner.

Dette er et research-verktøy, ikke investeringsråd.

## Videre ideer

Se [docs/ROADMAP.md](docs/ROADMAP.md).
