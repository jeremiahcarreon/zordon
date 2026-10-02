from __future__ import annotations

import xml.etree.ElementTree as ET

import segno

from zordon.transport.qr import svg_qr, terminal_qr

URL = "https://quiet-ocean-example-1234.trycloudflare.com"


def test_terminal_qr_is_compact_text():
    out = terminal_qr(URL)
    assert out
    rows = [r for r in out.splitlines() if r]
    # compact (half-block) rendering: roughly half the symbol height, all rows equal width
    size = segno.make(URL, error="m").symbol_size(scale=1, border=1)[1]
    assert size // 2 <= len(rows) <= size // 2 + 2
    assert len({len(r) for r in rows}) == 1
    assert any(ch in out for ch in ("▀", "▄", "█", " "))


def test_svg_qr_parses_and_has_no_xml_declaration():
    svg = svg_qr(URL)
    assert svg.startswith("<svg")
    assert "<?xml" not in svg
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    assert "viewBox" in root.attrib or "width" in root.attrib
    # scale 4 with a 1-module border: width = (modules + 2) * 4
    modules = segno.make(URL, error="m").symbol_size(scale=1, border=0)[0]
    assert int(float(root.attrib["width"])) == (modules + 2) * 4
    assert any(child.tag.endswith("path") for child in root.iter())


def test_svg_qr_scale_changes_size():
    small = ET.fromstring(svg_qr(URL, scale=2))
    big = ET.fromstring(svg_qr(URL, scale=8))
    assert int(float(big.attrib["width"])) == 4 * int(float(small.attrib["width"]))
