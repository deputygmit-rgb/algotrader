from src.mcp_server.annual_report_fetcher import _extract_screener_financials


def test_extract_screener_financials_includes_ratios_statements_ownership_and_peers():
    page = """
    <h1>Example Industries</h1>
    <ul id="top-ratios">
      <li><span class="name">Stock P/E</span><span class="value">24.5</span></li>
    </ul>
    <section id="quarters"><table>
      <thead><tr><th></th><th>Mar 2025</th><th>Jun 2025</th></tr></thead>
      <tbody><tr><td>EPS</td><td>10</td><td>12</td></tr></tbody>
    </table></section>
    <section id="profit-loss"><table>
      <thead><tr><th></th><th>FY 2025</th></tr></thead>
      <tbody><tr><td>Net Profit</td><td>500</td></tr></tbody>
    </table></section>
    <section id="shareholding"><table>
      <thead><tr><th></th><th>Jun 2025</th></tr></thead>
      <tbody><tr><td>Promoters</td><td>51%</td></tr></tbody>
    </table></section>
    <section id="peers"><table>
      <thead><tr><th>Company</th><th>Stock P/E</th></tr></thead>
      <tbody><tr><td>Example Industries</td><td>24.5</td></tr></tbody>
    </table></section>
    """

    result = _extract_screener_financials(page)

    assert result["company_name"] == "Example Industries"
    assert result["Stock P/E"] == "24.5"
    assert result["quarterly_results"]["rows"][0] == {
        "metric": "EPS",
        "values": ["10", "12"],
    }
    assert result["profit_loss"]["rows"][0]["metric"] == "Net Profit"
    assert result["shareholding_pattern"]["rows"][0]["values"] == ["51%"]
    assert result["peers"]["rows"][0]["values"] == ["24.5"]


def test_extract_screener_financials_discards_unit_only_values():
    page = """
    <h1>Example Industries</h1>
    <ul id="top-ratios">
      <li><span class="name">Market Cap</span><span class="value">₹ 58,318 Cr.</span></li>
      <li><span class="name">Current Price</span><span class="value">₹</span><span class="number">1,470</span></li>
      <li><span class="name">Book Value</span><span class="value">₹</span></li>
      <li><span class="name">ROCE</span><span class="value">%</span></li>
      <li><span class="name">Stock P/E</span><span class="value">—</span></li>
    </ul>
    <section id="quarters"><table>
      <thead><tr><th></th><th>Mar 2025</th><th>Jun 2025</th></tr></thead>
      <tbody>
        <tr><td>EPS</td><td>10</td><td>12</td></tr>
        <tr><td>Unavailable ratio</td><td>%</td><td>—</td></tr>
      </tbody>
    </table></section>
    """

    result = _extract_screener_financials(page)

    assert result["Market Cap"] == "₹ 58,318 Cr."
    assert result["Current Price"] == "1,470"
    assert "Book Value" not in result
    assert "ROCE" not in result
    assert "Stock P/E" not in result
    assert [row["metric"] for row in result["quarterly_results"]["rows"]] == ["EPS"]
