# Roadmap

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
- [ ] Kurs fra en ekstern kilde (f.eks. FMP-gratisnivå eller Stooq) så vi kan regne P/E, EV/omsetning og PEG
- [ ] Analytikerdekning som mål på hvor «under radaren» et selskap er

## Drift
- [ ] Persistent cache (f.eks. SQLite på disk) så frames ikke lastes ned på nytt etter omstart
- [ ] Logging av hvilke verktøy som brukes, for å se hva som faktisk er nyttig
