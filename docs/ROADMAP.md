# Roadmap

## Neste: fra første screening (arbeidsflyt B, 25.11.2026)
Mye av frasorteringen ble gjort manuelt. Disse skal inn i `screen_companies`:
- [x] **Bransjefilter via SIC-kode** (fra submissions): `exclude_sectors` utelater som standard
      funds_trusts, banks_finance, insurance, real_estate, shipping og biotech_pharma (`[]` = alle med).
      Hvert treff får `sic` og `industry`; utelatte listes i `excluded_by_sector`. Bare treff som består de
      andre filtrene slås opp (~8 per sekund), og SIC holdes i minnet så lenge serveren kjører
- [x] **Urimelige marginer**: `exclude_suspect` (standard på) utelater og lister under `excluded_suspect`:
      netto margin > 60 %, negativ bruttomargin, nettoresultat > 1,5 × driftsresultat eller driftsunderskudd
      (NFE, EOSE, LTC), positivt resultat men EPS ≤ 0 (EOSE) og P/E < 3
- [x] **P/E i screeningen** på siste 12 mnd EPS i stedet for ett kvartal × 4 (VATE), med EPS også fra
      `EarningsPerShareBasicAndDiluted` (EOSE/NFE manglet EPS, og koden falt tilbake til markedsverdi/nettoresultat).
      Manglende Q4 avledes fra helår; avvikende regnskapsår gir «3 quarters annualized» (`pe_basis`)
- [ ] **Jevn vekst**: krav om vekst over terskel i minst N av de siste kvartalene, ikke bare ett
      (skilte FPS/INOD/PAYS fra AEHR/VICR/SMTC)
- [ ] **Innsidesignal**: valgfritt filter «minst X innsidere har kjøpt på åpent marked siste Y dager»
      (FPS: 6 innsidere kjøpte etter 70 % kursfall)

Funn fra første screening med verdsettelse (25.11.2026, 15 treff, 11 forkastet manuelt):
- [x] **Trakt steg 1–2 som kode** (`python -m sec_edgar_mcp.funnel`), regler i stockdocs/method. Rettet samtidig:
      TTM-EPS (nettoresultat / aksjer), DEF 14A ignoreres, engangsposter sjekkes i alle fire kvartaler (steg 2)
- [x] **TTM-EPS på tvers av aksjesplitt/IPO** (get_quote og steg 2; screen_companies' P/E har fortsatt feilen): avledet Q4 = helårs-EPS minus kvartaler med et annet aksjetall
      (CLMB 4:1-splitt ga Q4-EPS 3,86 og P/E 8,7, riktig ca. 32; OMDA etter IPO ga Q4-EPS 1,10, riktig ca. 0,09).
      Avled Q4-EPS fra nettoresultat / utvannede aksjer, eller dropp Q4 når aksjetallet spriker mer enn 1,5x
- [x] **Engangsposter i eldre TTM-kvartaler** (i steg 2; screen_companies ser fortsatt bare siste kvartal): sanity-sjekken ser bare siste kvartal. AMSC (skattefordel 118M i
      Q4 2025, P/E 5,2 mot reelt ca. 45) og QNST (Q2 nettoresultat 50M mot driftsresultat 1M) slapp gjennom.
      Sjekk alle fire kvartaler (driftsresultat-frames for hvert kvartal) eller sammenlign TTM-EPS med TTM-driftsresultat
- [x] **DEF 14A i get_financials**: «pay versus performance»-tabeller i proxy-rapporter blir lest som årstall
      (QNST: omsetning 112M og bruttomargin 130 %; FRD: årsrad uten omsetning, get_quote ga P/E None).
      Bruk bare fakta fra 10-K/10-Q/20-F/40-F
- [ ] Screening: flagg når veksten skyldes oppkjøp (8-K 2.01 siste 12 mnd) eller én kunde (FRD, PLOW, LEGH)

Andre funn fra samme kjøring:
- [ ] `get_insider_trades`: merke sekundærsalg fra oppkjøpsfond (pris mangler, value_usd vises som 0)
- [ ] `get_financials`: flagge utdatert `shares_outstanding` (VICR viste tall fra 2015)
- [ ] `get_financials`: varsle når et kvartal mangler i rekken (SMTC manglet Q2 FY27)
- [ ] `get_financials`: årsrader skal bare komme fra helårstall (fp=FY / 10-K). Amazon tagger 12-måneders
      kontantstrøm i 10-Q, som nå vises som «årsrader» med fiscal_period Q1-Q3

## Nye datakilder (fra FPS-analysen)
- [x] FRED-modul (renter, byggeaktivitet, makro), gratis API-nøkkel
- [x] EIA-modul (strømforbruk, etterspørsel fra datasentre), gratis API-nøkkel
- [ ] Ferdig verktøy for kundenes capex (hyperskalere) og konkurrentsammenligning

## v0.2: bedre screening
- [ ] Siste fire kvartaler (TTM) i stedet for ett kvartal, så sesongsvingninger jevnes ut
- [ ] Vekstakselerasjon: vekst siste kvartal mot vekst kvartalet før
- [ ] Filtrere bort selskaper med stor utvanning (antall aksjer opp mer enn X % på ett år)
- [ ] Gjeld/egenkapital og kontantstrøm i screeningen
- [x] Bransjefilter via SIC-kode fra submissions (se over)

## v0.3: faresignaler automatisk
- [ ] `red_flags(company)`: samler revisorskifte (8-K 4.01), restatement (8-K 4.02), forsinkede rapporter (NT 10-K/10-Q),
      "material weakness", "going concern", kundekonsentrasjon og emisjoner (S-3/424B) i én oversikt
- [ ] Varelager og kundefordringer som vokser raskere enn omsetningen

## v0.4: verdsettelse
- [x] Kurs fra ekstern kilde (Massive gratisplan, sluttkurs; Finnhub for sanntid): `get_quote`, `get_price_history`
  (Nasdaqs uoffisielle API ble prøvd og blokkerer med 403 «Access Denied»)
- [x] P/E, P/S og P/FCF på siste fire kvartaler, markedsverdi med alle aksjeklasser
- [x] Aksjeklasser leses fra forsiden i siste 10-Q/10-K (inline XBRL, `StatementClassOfStockAxis`), fordi
      companyfacts dropper tall med dimensjon (FPS, GOOGL). Varsel når forsideaksjer og utvannede aksjer spriker
- [x] `get_price_history`: endring regnes mot kalenderdato (1y var None ved nøyaktig 252 handelsdager)
- [ ] P/S i screeningen på TTM-omsetning (nå kvartal × 4)
- [x] Verdsettelse i `screen_companies` (`max_pe`, markedsverdifilter, sortering på P/E)
- [ ] EV (netto gjeld) og PEG i `get_quote`
- [ ] Screeningens markedsverdi bruker SEC-frames (én aksjeklasse for A/B-selskaper): summere klasser for treffene
- [ ] Analytikermål og analytikerdekning (Finnhub `recommendation`)
- [ ] Analytikerdekning som mål på hvor «under radaren» et selskap er

## Drift
- [ ] Persistent cache (f.eks. SQLite på disk) så frames ikke lastes ned på nytt etter omstart
- [ ] Logging av hvilke verktøy som brukes, for å se hva som faktisk er nyttig
