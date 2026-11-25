"""A fake SEC EDGAR served through httpx2.MockTransport, shaped like the real endpoints."""

from __future__ import annotations

import json

import httpx2

ACME = 1000001
OTC_CO = 1000002
SHRINK = 1000003
SWITCHER = 1000004
TINY = 1000005
GAIN = 1000006
LAST_SEARCH_PARAMS: dict = {}

TICKERS = {
    "fields": ["cik", "name", "ticker", "exchange"],
    "data": [
        [ACME, "Acme Robotics Inc.", "ACME", "Nasdaq"],
        [ACME, "Acme Robotics Inc.", "ACME-W", "Nasdaq"],
        [OTC_CO, "Tiny Pink Sheet Corp", "TPSC", "OTC"],
        [SHRINK, "Shrinking Widgets Co", "SHRK", "NYSE"],
        [SWITCHER, "Switcher Tag Holdings", "SWTH", "NYSE"],
        [TINY, "Tiny Base Biotech", "TINY", "Nasdaq"],
        [GAIN, "Gain On Debt Corp", "GAIN", "NYSE"],
    ],
}


def frame(rows):
    return {"taxonomy": "us-gaap", "data": rows}


def row(cik, name, val, end="2026-09-30", accn="0001-26-000001"):
    return {"accn": accn, "cik": cik, "entityName": name, "loc": "US-CA", "end": end, "val": val}


FRAMES = {
    ("Revenues", "CY2026Q3"): frame([
        row(ACME, "Acme Robotics Inc.", 300e6),
        row(OTC_CO, "Tiny Pink Sheet Corp", 90e6),
        row(SHRINK, "Shrinking Widgets Co", 50e6),
        row(9999999, "Unlisted Fund LP", 500e6),
        row(TINY, "Tiny Base Biotech", 50e6),  # one-off licence fee
        row(GAIN, "Gain On Debt Corp", 100e6),
    ]),
    ("Revenues", "CY2025Q3"): frame([
        row(ACME, "Acme Robotics Inc.", 150e6, end="2025-09-30"),
        row(OTC_CO, "Tiny Pink Sheet Corp", 30e6, end="2025-09-30"),
        row(SHRINK, "Shrinking Widgets Co", 80e6, end="2025-09-30"),
        row(9999999, "Unlisted Fund LP", 100e6, end="2025-09-30"),
        row(TINY, "Tiny Base Biotech", 1e6, end="2025-09-30"),
        row(GAIN, "Gain On Debt Corp", 60e6, end="2025-09-30"),
    ]),
    # Operating income: GAIN loses money in the business; its profit is a gain on debt (like NFE/EOSE)
    ("OperatingIncomeLoss", "CY2026Q3"): frame([
        row(ACME, "Acme Robotics Inc.", 40e6),
        row(SWITCHER, "Switcher Tag Holdings", 15e6),
        row(GAIN, "Gain On Debt Corp", -5e6),
    ]),
    # Switcher only uses the ASC 606 tag
    ("RevenueFromContractWithCustomerExcludingAssessedTax", "CY2026Q3"): frame([
        row(SWITCHER, "Switcher Tag Holdings", 120e6),
        row(ACME, "Acme Robotics Inc.", 1.0),  # must lose to the 'Revenues' tag
    ]),
    ("RevenueFromContractWithCustomerExcludingAssessedTax", "CY2025Q3"): frame([
        row(SWITCHER, "Switcher Tag Holdings", 80e6, end="2025-09-30"),
    ]),
    ("NetIncomeLoss", "CY2026Q3"): frame([
        row(ACME, "Acme Robotics Inc.", 30e6),
        row(OTC_CO, "Tiny Pink Sheet Corp", 5e6),
        row(SHRINK, "Shrinking Widgets Co", 1e6),
        row(SWITCHER, "Switcher Tag Holdings", 12e6),
        row(9999999, "Unlisted Fund LP", 50e6),
        row(TINY, "Tiny Base Biotech", 40e6),
        row(GAIN, "Gain On Debt Corp", 20e6),
    ]),
    ("NetIncomeLoss", "CY2025Q3"): frame([
        row(ACME, "Acme Robotics Inc.", 10e6, end="2025-09-30"),
        row(SWITCHER, "Switcher Tag Holdings", -2e6, end="2025-09-30"),
    ]),
    ("GrossProfit", "CY2026Q3"): frame([row(ACME, "Acme Robotics Inc.", 120e6)]),
    # Used by the screener's valuation
    ("EarningsPerShareDiluted", "CY2026Q3"): frame([
        row(ACME, "Acme Robotics Inc.", 0.60),
        row(SWITCHER, "Switcher Tag Holdings", 0.24),  # only this quarter: falls back to 1 quarter x 4
    ]),
    ("EarningsPerShareDiluted", "CY2026Q2"): frame([row(ACME, "Acme Robotics Inc.", 0.55, end="2026-06-30")]),
    ("EarningsPerShareDiluted", "CY2026Q1"): frame([row(ACME, "Acme Robotics Inc.", 0.50, end="2026-03-31")]),
    # No CY2025Q4 frame for ACME: Q4 = full year 1.20 - (0.20 + 0.25 + 0.30) = 0.45
    ("EarningsPerShareDiluted", "CY2025"): frame([row(ACME, "Acme Robotics Inc.", 1.20, end="2025-12-31")]),
    ("EarningsPerShareDiluted", "CY2025Q1"): frame([row(ACME, "Acme Robotics Inc.", 0.20, end="2025-03-31")]),
    ("EarningsPerShareDiluted", "CY2025Q2"): frame([row(ACME, "Acme Robotics Inc.", 0.25, end="2025-06-30")]),
    ("EarningsPerShareDiluted", "CY2025Q3"): frame([row(ACME, "Acme Robotics Inc.", 0.30, end="2025-09-30")]),
    # GAIN tags only the combined concept, and its gain is excluded from EPS
    ("EarningsPerShareBasicAndDiluted", "CY2026Q3"): frame([row(GAIN, "Gain On Debt Corp", -0.05)]),
    # Cover-page share counts are dated after quarter end, so most land in the next quarter's frame
    ("EntityCommonStockSharesOutstanding", "CY2026Q4I"): frame([
        row(ACME, "Acme Robotics Inc.", 50e6, end="2026-10-31"),
    ]),
    ("EntityCommonStockSharesOutstanding", "CY2026Q3I"): frame([
        row(ACME, "Acme Robotics Inc.", 45e6, end="2026-07-31"),  # older count: must lose
        row(SWITCHER, "Switcher Tag Holdings", 50e6),
    ]),
}


def fact(start, end, val, filed, form="10-Q", fy=2026, fp="Q1", accn="0001-26-000010"):
    f = {"end": end, "val": val, "accn": accn, "fy": fy, "fp": fp, "form": form, "filed": filed}
    if start:
        f["start"] = start
    return f


ACME_FACTS = {
    "cik": ACME,
    "entityName": "Acme Robotics Inc.",
    "facts": {
        "dei": {
            "EntityCommonStockSharesOutstanding": {"units": {"shares": [
                fact(None, "2026-10-31", 50_000_000, "2026-11-05"),
            ]}},
        },
        "us-gaap": {
            # Older years tagged SalesRevenueNet, newer years Revenues
            "SalesRevenueNet": {"units": {"USD": [
                fact("2024-01-01", "2024-12-31", 400e6, "2025-02-20", form="10-K", fy=2024, fp="FY"),
            ]}},
            "Revenues": {"units": {"USD": [
                fact("2025-01-01", "2025-12-31", 700e6, "2026-02-20", form="10-K", fy=2025, fp="FY"),
                fact("2025-01-01", "2025-03-31", 120e6, "2025-05-05", fy=2025, fp="Q1"),
                fact("2025-04-01", "2025-06-30", 140e6, "2025-08-05", fy=2025, fp="Q2"),
                fact("2025-01-01", "2025-06-30", 260e6, "2025-08-05", fy=2025, fp="Q2"),  # YTD: ignored
                fact("2025-07-01", "2025-09-30", 150e6, "2025-11-05", fy=2025, fp="Q3"),
                fact("2025-07-01", "2025-09-30", 155e6, "2026-11-05", fy=2026, fp="Q3"),  # restated later
                fact("2026-01-01", "2026-03-31", 220e6, "2026-05-05", fy=2026, fp="Q1"),
                fact("2026-04-01", "2026-06-30", 260e6, "2026-08-05", fy=2026, fp="Q2"),
                fact("2026-07-01", "2026-09-30", 300e6, "2026-11-05", fy=2026, fp="Q3"),
            ]}},
            "NetIncomeLoss": {"units": {"USD": [
                fact("2025-01-01", "2025-12-31", 60e6, "2026-02-20", form="10-K", fy=2025, fp="FY"),
                fact("2025-07-01", "2025-09-30", 10e6, "2025-11-05", fy=2025, fp="Q3"),
                fact("2026-07-01", "2026-09-30", 30e6, "2026-11-05", fy=2026, fp="Q3"),
            ]}},
            "EarningsPerShareDiluted": {"units": {"USD/shares": [
                fact("2025-01-01", "2025-12-31", 1.20, "2026-02-20", form="10-K", fy=2025, fp="FY"),
                fact("2025-01-01", "2025-03-31", 0.20, "2025-05-05", fy=2025, fp="Q1"),
                fact("2025-04-01", "2025-06-30", 0.25, "2025-08-05", fy=2025, fp="Q2"),
                fact("2025-07-01", "2025-09-30", 0.30, "2025-11-05", fy=2025, fp="Q3"),
                fact("2026-07-01", "2026-09-30", 0.6, "2026-11-05", fy=2026, fp="Q3"),
            ]}},
            "InventoryNet": {"units": {"USD": [
                fact(None, "2025-12-31", 80e6, "2026-02-20", form="10-K"),
                fact(None, "2026-09-30", 140e6, "2026-11-05"),
            ]}},
            "LongTermDebt": {"units": {"USD": [
                fact(None, "2024-03-31", 85e6, "2024-05-05"),  # tag abandoned since
            ]}},
            "ConvertibleNotesPayable": {"units": {"USD": [
                fact(None, "2026-09-30", 2000e6, "2026-11-05"),
            ]}},
            "LongTermDebtCurrent": {"units": {"USD": [
                fact(None, "2026-09-30", 100e6, "2026-11-05"),
            ]}},
            "DebtInstrumentFaceAmount": {"units": {"USD": [
                fact(None, "2026-09-30", 2100e6, "2026-11-05"),
            ]}},
            "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
                fact(None, "2026-09-30", 95e6, "2026-11-05"),
            ]}},
        },
    },
}

SUBMISSIONS = {
    "cik": str(ACME),
    "name": "Acme Robotics Inc.",
    "tickers": ["ACME"],
    "sicDescription": "Electronic Computers",
    "fiscalYearEnd": "1231",
    "filings": {"recent": {
        "accessionNumber": ["0001-26-000050", "0001-26-000049", "0001-26-000048", "0001-26-000047",
                            "0001-26-000046", "0001-26-000040", "0001-26-000030", "0001-24-000001"],
        "filingDate": ["2026-11-10", "2026-11-10", "2026-11-08", "2026-11-05",
                       "2026-11-04", "2026-11-04", "2026-02-20", "2024-01-15"],
        "reportDate": ["2026-11-06", "2026-11-06", "2026-11-06", "2026-09-30",
                       "2026-11-04", "2026-11-03", "2025-12-31", "2024-01-12"],
        "form": ["4", "4", "4", "10-Q", "8-K", "4", "10-K", "4"],
        "primaryDocument": ["xslF345X05/form4a.xml", "xslF345X05/form4b.xml", "xslF345X05/form4c.xml",
                            "acme-20260930.htm", "acme-8k.htm", "xslF345X05/form4d.xml",
                            "acme-20251231.htm", "xslF345X05/old.xml"],
        "primaryDocDescription": ["", "", "", "10-Q", "8-K", "", "10-K", ""],
        "items": ["", "", "", "", "2.02,9.01", "", "", ""],
    }},
}


def form4(owner, roles_xml, transactions_xml, plan="0"):
    return f"""<?xml version="1.0"?>
<ownershipDocument>
  <periodOfReport>2026-11-06</periodOfReport>
  <aff10b5One>{plan}</aff10b5One>
  <issuer><issuerCik>{ACME}</issuerCik><issuerTradingSymbol>ACME</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>{owner}</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>{roles_xml}</reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>{transactions_xml}</nonDerivativeTable>
</ownershipDocument>"""


def tx(date, code, shares, price, ad, after):
    return f"""<nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>{date}</value></transactionDate>
      <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>{code}</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>{shares}</value></transactionShares>
        <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>{after}</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
    </nonDerivativeTransaction>"""


SALE = tx("2026-11-06", "S", 100000, 40.0, "D", 5000000)
FORM4 = {
    "form4a.xml": form4("Doe Jane", "<isOfficer>1</isOfficer><officerTitle>CEO</officerTitle>", SALE, plan="1"),
    "form4b.xml": form4("Doe John", "<isDirector>1</isDirector>", SALE),  # same trade, joint owner
    "form4c.xml": form4("Smith Ann", "<isDirector>true</isDirector>",
                        tx("2026-11-06", "P", 2000, 25.5, "A", 12000) + tx("2026-11-06", "F", 500, 25.5, "D", 11500)),
    "form4d.xml": form4("Lee Bo", "<isOfficer>1</isOfficer><officerTitle>CFO</officerTitle>",
                        tx("2026-11-03", "A", 10000, 0, "A", 10000)),
}

TEN_K_HTML = """<html><head><title>10-K</title></head><body>
<div style="display:none"><ix:header>HIDDEN XBRL FACTS SHOULD NOT APPEAR</ix:header></div>
<p>TABLE OF CONTENTS</p>
<table>
<tr><td>Item 1.</td><td>Business</td><td>3</td></tr>
<tr><td>Item 1A.</td><td>Risk Factors</td><td>10</td></tr>
<tr><td>Item 1B.</td><td>Unresolved Staff Comments</td><td>25</td></tr>
<tr><td>Item 2.</td><td>Properties</td><td>26</td></tr>
</table>
<h2>Item 1. Business</h2>
<p>Acme designs rack-scale robots for AI data centers. """ + ("We grow quickly. " * 40) + """</p>
<h2>Item 1A. Risk Factors</h2>
<p>One customer accounted for 41% of net sales.</p>
<p>A single supplier provides 63% of our components. """ + ("Risks are real. " * 60) + """</p>
<h2>Item 1B. Unresolved Staff Comments</h2><p>None.</p>
<h2>Item 2. Properties</h2><p>San Jose.</p>
<h2>Item 8. Financial Statements and Supplementary Data</h2>
<p>Report of Independent Registered Public Accounting Firm</p>
<p>Opinion on the Financial Statements. In our opinion, the consolidated financial statements present fairly, in all
material respects, the financial position of Acme. """ + ("Audit detail. " * 80) + """</p>
<p>Report of Independent Registered Public Accounting Firm</p>
<p>Adverse Opinion on Internal Control over Financial Reporting. In our opinion, because of the effect of the material
weakness, Acme has not maintained effective internal control. """ + ("Control detail. " * 80) + """</p>
<h2>Item 9. Changes in and Disagreements with Accountants</h2><p>None.</p>
</body></html>"""

# Inline-XBRL 10-Q cover page: two share classes (one with scale=6), a duplicate of the Class A fact and an
# undimensioned total that must not be added on top of the classes.
TEN_Q_HTML = """<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body>
<div style="display:none"><ix:header><ix:resources>
<xbrli:context id="c-1"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0001000001</xbrli:identifier>
</xbrli:entity><xbrli:period><xbrli:instant>2026-10-31</xbrli:instant></xbrli:period></xbrli:context>
<xbrli:context id="c-2"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0001000001</xbrli:identifier>
<xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementClassOfStockAxis">us-gaap:CommonClassAMember</xbrldi:explicitMember></xbrli:segment>
</xbrli:entity><xbrli:period><xbrli:instant>2026-10-31</xbrli:instant></xbrli:period></xbrli:context>
<xbrli:context id="c-3"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0001000001</xbrli:identifier>
<xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementClassOfStockAxis">us-gaap:CommonClassBMember</xbrldi:explicitMember></xbrli:segment>
</xbrli:entity><xbrli:period><xbrli:instant>2026-10-31</xbrli:instant></xbrli:period></xbrli:context>
</ix:resources></ix:header></div>
<p>As of October 31, 2026, the registrant had
<ix:nonFraction unitRef="shares" contextRef="c-2" decimals="INF" name="dei:EntityCommonStockSharesOutstanding" format="ixt:num-dot-decimal" scale="0">42,000,000</ix:nonFraction>
shares of Class A common stock and
<ix:nonFraction unitRef="shares" contextRef="c-3" decimals="-5" name="dei:EntityCommonStockSharesOutstanding" format="ixt:num-dot-decimal" scale="6">8.0</ix:nonFraction>
million shares of Class B common stock outstanding
(<ix:nonFraction unitRef="shares" contextRef="c-1" decimals="INF" name="dei:EntityCommonStockSharesOutstanding" scale="0">50,000,000</ix:nonFraction> in total).</p>
<p>Class A again: <ix:nonFraction unitRef="shares" contextRef="c-2" decimals="INF" name="dei:EntityCommonStockSharesOutstanding" scale="0"><span>42,000,000</span></ix:nonFraction></p>
</body></html>"""

EIGHT_K_HTML = "<html><body><p>Item 2.02 Results of Operations. See Exhibit 99.1.</p></body></html>"
EX99_HTML = "<html><body><p>Acme reports record revenue of $300 million, up 100% year over year.</p></body></html>"
EIGHT_K_INDEX_PAGE = """<?xml version="1.0"?><html><body>
<table class="tableFile" summary="Document Format Files">
<tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
<tr><td>1</td><td>8-K</td><td><a href="/ix?doc=/Archives/edgar/data/1000001/000126000046/acme-8k.htm">acme-8k.htm</a></td><td>8-K</td><td>30000</td></tr>
<tr><td>2</td><td>PRESS RELEASE</td><td><a href="/Archives/edgar/data/1000001/000126000046/q3fy26results.htm">q3fy26results.htm</a></td><td>EX-99.1</td><td>90000</td></tr>
<tr><td>3</td><td>GRAPHIC</td><td><a href="/Archives/edgar/data/1000001/000126000046/logo.jpg">logo.jpg</a></td><td>GRAPHIC</td><td>5000</td></tr>
</table></body></html>"""
EIGHT_K_INDEX = {"directory": {"name": "/Archives/edgar/data/1000001/000126000046", "item": [
    {"name": "acme-8k.htm", "type": "text.gif"},
    {"name": "acme-ex991.htm", "type": "text.gif"},
    {"name": "R1.htm", "type": "text.gif"},
]}}

SEARCH = {"hits": {"total": {"value": 2}, "hits": [
    {"_id": "0001-26-000046:acme-ex991.htm", "_source": {
        "ciks": ["0001000001"], "display_names": ["Acme Robotics Inc.  (ACME, ACME-W)  (CIK 0001000001)"],
        "form": "8-K", "root_forms": ["8-K"], "file_date": "2026-11-04", "period_ending": "2026-11-03"}},
    {"_id": "0002-26-000001:x-ex991.htm", "_source": {
        "ciks": ["0001000003"], "display_names": ["Shrinking Widgets Co  (SHRK)  (CIK 0001000003)"],
        "form": "8-K", "file_date": "2026-10-01"}},
]}}


def handler(request: httpx2.Request) -> httpx2.Response:
    url = str(request.url)
    path = request.url.path
    if "User-Agent" not in request.headers or "@" not in request.headers["User-Agent"]:
        return httpx2.Response(403)

    def ok(payload):
        if isinstance(payload, (dict, list)):
            return httpx2.Response(200, content=json.dumps(payload).encode(), headers={"content-type": "application/json"})
        return httpx2.Response(200, content=payload.encode(), headers={"content-type": "text/html"})

    if path == "/files/company_tickers_exchange.json":
        return ok(TICKERS)
    if path.startswith("/api/xbrl/frames/"):
        _, _, _, _, _, concept, _unit, period = path.split("/")
        payload = FRAMES.get((concept, period.removesuffix(".json")))
        return ok(payload) if payload else httpx2.Response(404)
    if path == f"/api/xbrl/companyfacts/CIK{ACME:010d}.json":
        return ok(ACME_FACTS)
    if path == f"/submissions/CIK{ACME:010d}.json":
        return ok(SUBMISSIONS)
    if path.startswith(f"/Archives/edgar/data/{ACME}/"):
        filename = path.rsplit("/", 1)[1]
        if filename in FORM4:
            return ok(FORM4[filename])
        if filename == "acme-20251231.htm":
            return ok(TEN_K_HTML)
        if filename == "acme-20260930.htm":
            return ok(TEN_Q_HTML)
        if filename == "acme-8k.htm":
            return ok(EIGHT_K_HTML)
        if filename in ("acme-ex991.htm", "q3fy26results.htm"):
            return ok(EX99_HTML)
        if filename == "0001-26-000046-index.htm":
            return ok(EIGHT_K_INDEX_PAGE)
        if filename == "index.json":
            return ok(EIGHT_K_INDEX)
    if "efts.sec.gov" in url and path.endswith("/search-index"):
        LAST_SEARCH_PARAMS.clear()
        LAST_SEARCH_PARAMS.update(dict(request.url.params))
        return ok(SEARCH)
    return httpx2.Response(404)


def transport() -> httpx2.MockTransport:
    return httpx2.MockTransport(handler)
