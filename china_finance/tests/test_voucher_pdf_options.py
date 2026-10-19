import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from frappe.utils.pdf import get_print_format_styles


@pytest.mark.parametrize(
	("name", "paper", "orientation"),
	[("china_accounting_voucher", "A4", "Portrait"),
	 ("china_accounting_voucher_a5_landscape", "A5", "Landscape")],
)
def test_voucher_paper_options_are_readable_by_frappe(name, paper, orientation):
	base = Path(__file__).parents[1] / "china_finance" / "print_format" / name / name
	html = base.with_suffix(".html").read_text()
	metadata = json.loads(base.with_suffix(".json").read_text())
	assert metadata["html"] == html
	styles = {style.name: style.value for style in get_print_format_styles(BeautifulSoup(html, "html.parser"))}
	assert styles["page-size"] == paper
	assert styles["orientation"] == orientation
