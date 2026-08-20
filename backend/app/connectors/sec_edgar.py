"""SEC EDGAR: insider transactions (Form 4), free and structured -- not text to parse.

SEC's fair-access policy requires a descriptive User-Agent with contact info on every
request; this is set explicitly below and used everywhere in this module.

Only transactionCode "P" (open market purchase) and "S" (open market sale) are genuine
buy/sell signals. Everything else -- "A" (grant/award), "M" (option exercise), "G" (gift),
"F" (tax withholding), etc. -- reflects compensation mechanics, not an insider's market
view, and is filtered out. Verified against a real Apple filing that this distinction
matters: the first live Form 4 pulled for AAPL was transactionCode "M" (an option
exercise), which would have been wrongly counted as a bullish signal without this filter.
"""

import xml.etree.ElementTree as ET

import httpx

USER_AGENT = "MarketCopilot personal-use research tool contact:sebastien.saussus@gmail.com"
HEADERS = {"User-Agent": USER_AGENT}

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data"

OPEN_MARKET_BUY = "P"
OPEN_MARKET_SELL = "S"

_ticker_to_cik: dict[str, str] | None = None


def _load_ticker_map() -> dict[str, str]:
    global _ticker_to_cik
    if _ticker_to_cik is not None:
        return _ticker_to_cik
    try:
        with httpx.Client(timeout=15, headers=HEADERS) as client:
            resp = client.get(TICKER_MAP_URL)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError:
        return {}
    _ticker_to_cik = {row["ticker"].upper(): str(row["cik_str"]) for row in data.values()}
    return _ticker_to_cik


def get_cik(ticker: str) -> str | None:
    return _load_ticker_map().get(ticker.upper())


def get_recent_form4_accessions(cik: str, limit: int = 15) -> list[dict]:
    """Returns [{accession, filing_date, primary_document}] for the most recent Form 4s."""
    cik10 = cik.zfill(10)
    try:
        with httpx.Client(timeout=15, headers=HEADERS) as client:
            resp = client.get(SUBMISSIONS_URL.format(cik10=cik10))
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError:
        return []

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    results = []
    for i, form in enumerate(forms):
        if form != "4":
            continue
        results.append(
            {
                "accession": recent["accessionNumber"][i],
                "filing_date": recent["filingDate"][i],
            }
        )
        if len(results) >= limit:
            break
    return results


def _accession_dir_url(cik: str, accession: str) -> str:
    accession_no_dashes = accession.replace("-", "")
    cik_no_zeros = str(int(cik))
    return f"{ARCHIVE_BASE}/{cik_no_zeros}/{accession_no_dashes}"


def _resolve_filing_xml_url(client: httpx.Client, cik: str, accession: str) -> str | None:
    """The raw machine-readable Form 4 document's filename varies by filing agent --
    verified against two real filings that used different names ("form4.xml" for one
    filer, "wk-form4_....xml" for another). SEC's index.json lists the real files for
    an accession, so the filename is resolved dynamically rather than guessed.
    """
    dir_url = _accession_dir_url(cik, accession)
    try:
        resp = client.get(f"{dir_url}/index.json")
        resp.raise_for_status()
        items = resp.json().get("directory", {}).get("item", [])
    except httpx.HTTPError:
        return None

    for item in items:
        name = item.get("name", "")
        if name.endswith(".xml") and "index" not in name.lower():
            return f"{dir_url}/{name}"
    return None


def _parse_form4_xml(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    owner_name = root.findtext(".//reportingOwner/reportingOwnerId/rptOwnerName") or "Unknown"

    transactions = []
    for txn in root.findall(".//nonDerivativeTable/nonDerivativeTransaction"):
        code = txn.findtext("transactionCoding/transactionCode")
        if code not in (OPEN_MARKET_BUY, OPEN_MARKET_SELL):
            continue

        shares_raw = txn.findtext("transactionAmounts/transactionShares/value")
        price_raw = txn.findtext("transactionAmounts/transactionPricePerShare/value")
        date = txn.findtext("transactionDate/value")
        if not shares_raw or not price_raw:
            continue  # some transactions omit price (e.g. via footnote) -- skip rather than guess

        transactions.append(
            {
                "owner_name": owner_name,
                "transaction_code": code,
                "transaction_date": date,
                "shares": float(shares_raw),
                "price_per_share": float(price_raw),
            }
        )
    return transactions


def get_insider_transactions(ticker: str, lookback_filings: int = 15) -> list[dict] | None:
    """Open-market insider buy/sell transactions from the ticker's most recent Form 4
    filings, or None if the ticker/CIK isn't resolvable at all (vs. an empty list,
    which means resolvable but no open-market transactions recently)."""
    cik = get_cik(ticker)
    if cik is None:
        return None

    all_transactions = []
    with httpx.Client(timeout=15, headers=HEADERS) as client:
        for filing in get_recent_form4_accessions(cik, limit=lookback_filings):
            xml_url = _resolve_filing_xml_url(client, cik, filing["accession"])
            if xml_url is None:
                continue
            try:
                resp = client.get(xml_url)
                resp.raise_for_status()
            except httpx.HTTPError:
                continue
            all_transactions.extend(_parse_form4_xml(resp.text))

    return all_transactions
