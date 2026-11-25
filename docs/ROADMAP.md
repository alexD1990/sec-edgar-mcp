# Roadmap

## Neste: fra første screening (arbeidsflyt B, 25.11.2026)
Mye av frasorteringen ble gjort manuelt. Disse skal inn i `screen_companies`:
- [ ] **Bransjefilter via SIC-kode** (fra submissions): utelate fond, ETF-er, trusts, banker, forsikring,
      REIT-er, shipping og biotek/legemidler som standard, med mulighet for å slå dem på igjen
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
- [ ] Bransjefilter via SIC-kode fra submissions

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
