import httpx

from app.connectors.sec_edgar import HEADERS, _accession_dir_url, _parse_form4_xml, _resolve_filing_xml_url

# Trimmed from a real AAPL Form 4 filing (accession 0001140361-26-025622), kept close to
# the actual schema rather than a simplified fake. The M-coded transaction (option
# exercise) must be filtered out; only the P-coded one should survive.
SAMPLE_XML = """<?xml version="1.0"?>
<ownershipDocument>
    <issuer>
        <issuerCik>0000320193</issuerCik>
        <issuerName>Apple Inc.</issuerName>
        <issuerTradingSymbol>AAPL</issuerTradingSymbol>
    </issuer>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerCik>0001780525</rptOwnerCik>
            <rptOwnerName>Newstead Jennifer</rptOwnerName>
        </reportingOwnerId>
    </reportingOwner>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <transactionDate><value>2026-06-15</value></transactionDate>
            <transactionCoding>
                <transactionCode>M</transactionCode>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares><value>30104</value></transactionShares>
                <transactionPricePerShare><footnoteId id="F1"/></transactionPricePerShare>
            </transactionAmounts>
        </nonDerivativeTransaction>
        <nonDerivativeTransaction>
            <transactionDate><value>2026-06-16</value></transactionDate>
            <transactionCoding>
                <transactionCode>P</transactionCode>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares><value>500</value></transactionShares>
                <transactionPricePerShare><value>202.50</value></transactionPricePerShare>
            </transactionAmounts>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
</ownershipDocument>
"""


def test_parse_form4_xml_filters_out_non_open_market_codes():
    transactions = _parse_form4_xml(SAMPLE_XML)
    # only the P-coded transaction should survive; the M (option exercise) is dropped
    assert len(transactions) == 1
    assert transactions[0]["transaction_code"] == "P"


def test_parse_form4_xml_extracts_correct_fields():
    transactions = _parse_form4_xml(SAMPLE_XML)
    txn = transactions[0]
    assert txn["owner_name"] == "Newstead Jennifer"
    assert txn["shares"] == 500.0
    assert txn["price_per_share"] == 202.50
    assert txn["transaction_date"] == "2026-06-16"


def test_parse_form4_xml_skips_transaction_with_missing_price():
    # the M-coded transaction has no direct price (it's behind a footnote) -- must not
    # crash, and since it's filtered by code anyway this also implicitly covers that path
    transactions = _parse_form4_xml(SAMPLE_XML)
    assert all(t["price_per_share"] is not None for t in transactions)


def test_parse_form4_xml_malformed_returns_empty():
    assert _parse_form4_xml("not xml at all") == []


def test_parse_form4_xml_empty_document():
    assert _parse_form4_xml("<ownershipDocument></ownershipDocument>") == []


def test_accession_dir_url_strips_dashes_and_leading_zeros():
    url = _accession_dir_url("0000320193", "0001140361-26-025622")
    assert url == "https://www.sec.gov/Archives/edgar/data/320193/000114036126025622"


def test_resolve_filing_xml_url_picks_the_data_file_not_index_or_txt(httpx_mock):
    # Regression test: an earlier version hardcoded the filename as "form4.xml", which
    # 404'd for a real NVDA filing that used "wk-form4_....xml" instead (different filing
    # agents name the document differently). The resolver must pick the actual .xml data
    # file from the directory listing, not guess a filename, and must skip index files
    # even though they also end in a path that could be mistaken for the data file.
    httpx_mock.add_response(
        url="https://www.sec.gov/Archives/edgar/data/1045810/000131026426000008/index.json",
        json={
            "directory": {
                "item": [
                    {"name": "0001310264-26-000008-index-headers.html"},
                    {"name": "0001310264-26-000008-index.html"},
                    {"name": "0001310264-26-000008.txt"},
                    {"name": "wk-form4_1786569187.xml"},
                ]
            }
        },
    )
    with httpx.Client(headers=HEADERS) as client:
        url = _resolve_filing_xml_url(client, "1045810", "0001310264-26-000008")
    assert url == "https://www.sec.gov/Archives/edgar/data/1045810/000131026426000008/wk-form4_1786569187.xml"


def test_resolve_filing_xml_url_returns_none_on_http_error(httpx_mock):
    httpx_mock.add_response(status_code=404)
    with httpx.Client(headers=HEADERS) as client:
        url = _resolve_filing_xml_url(client, "1045810", "0001310264-26-000008")
    assert url is None
