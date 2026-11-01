"""Generates the PDF fixtures used by tests/test_pdf_pipeline_golden.py.

Every word in these PDFs is original synthetic text written for this test
suite: no published paper, private document or third-party text is used.
Only the PDF standard fonts (Times, Helvetica, Courier) are referenced, and
standard fonts are never embedded, so the files contain no font software.

The committed PDF bytes are the fixture, not this script: the golden values
are tied to each file's SHA-256 in manifest.json. This script exists so the
files' provenance is reviewable and so they can be reproduced:

    python tests/fixtures/pdf/generate_fixtures.py           # rewrite PDFs + manifest
    python tests/fixtures/pdf/generate_fixtures.py --check   # regenerate in a temp dir and compare

Regenerating with a different reportlab version may change the bytes, which
invalidates the golden values. Do not regenerate casually.

reportlab's invariant mode fixes the creation date and document ID, so the
output is byte-for-byte repeatable for a given reportlab version.
"""
import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

import reportlab
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas

HERE = Path(__file__).resolve().parent
PAGE_WIDTH, PAGE_HEIGHT = letter
MARGIN = 72
BODY_TOP = PAGE_HEIGHT - MARGIN
BODY_BOTTOM = MARGIN
NOTICE = (
    "This document is synthetic test material written for the ResearchMind AI "
    "test suite. It describes no real study and contains no real data."
)


class FixtureOverflow(Exception):
    """A page's content did not fit its frames. Page assignment is part of
    what the tests check, so content never silently flows to another page."""


def _block(font, size, text, space_after=6, leading=None):
    return {"font": font, "size": size, "text": text, "space_after": space_after,
            "leading": leading or round(size * 1.3, 1)}


def heading(text):
    return _block("Helvetica-Bold", 11, text, space_after=4)


def body(text, font="Times-Roman", size=10.5):
    return _block(font, size, text)


def render(c, frames, blocks):
    """Draws blocks line by line through frames (x, top, width, bottom),
    in order. Raises FixtureOverflow if they do not all fit."""
    frame_index = 0
    x, y, width, bottom = frames[0]
    for block in blocks:
        lines = simpleSplit(block["text"], block["font"], block["size"], width)
        for line in lines:
            if y - block["leading"] < bottom:
                frame_index += 1
                if frame_index >= len(frames):
                    raise FixtureOverflow(block["text"][:60])
                x, y, width, bottom = frames[frame_index]
            y -= block["leading"]
            c.setFont(block["font"], block["size"])
            c.drawString(x, y, line)
        y -= block["space_after"]
    return y


def footer(c, text):
    c.setFont("Helvetica", 8)
    c.drawCentredString(PAGE_WIDTH / 2, 40, text)


def new_canvas(path, title):
    c = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    c.setTitle(title)
    c.setAuthor("ResearchMind AI test suite (synthetic fixture)")
    c.setSubject("Synthetic PDF fixture; no real study or data")
    c.setCreator("tests/fixtures/pdf/generate_fixtures.py")
    return c


FULL_WIDTH = [(MARGIN, BODY_TOP, PAGE_WIDTH - 2 * MARGIN, BODY_BOTTOM)]


def build_single_column(path):
    """Three pages: a long first page, a longer second page that splits into
    several chunks, and a deliberately short third page that indexing skips
    (under 100 characters once cleaned). Footers read "Page N", which
    clean_text removes."""
    c = new_canvas(path, "Seasonal Nectar Availability in Urban Apiaries")
    render(c, FULL_WIDTH, [
        _block("Helvetica-Bold", 16, "Seasonal Nectar Availability in Urban Apiaries", space_after=4),
        _block("Helvetica-Oblique", 10, "A synthetic benchmark document for PDF extraction tests"),
        _block("Helvetica", 10, "A. Fictional, B. Placeholder and C. Example, Department of Imaginary Studies"),
        _block("Helvetica", 9, NOTICE + " Fixture marker RMFX-SC-P1.", space_after=10),
        heading("Abstract"),
        body(
            "Urban beekeepers often report that honey yields rise and fall with the flowering "
            "calendar of nearby streets, parks and gardens, yet few records describe how that "
            "calendar shifts from one season to the next. This synthetic document imagines a "
            "two-year survey of forty rooftop apiaries and describes, in deliberately ordinary "
            "prose, how nectar availability might be estimated from weekly hive weights, local "
            "temperature and a simple inventory of flowering plants. None of the numbers in this "
            "text were measured. They exist so that a text extractor and a chunker have realistic "
            "sentences, headings and paragraph breaks to work on, and so that any change in how "
            "those tools behave can be noticed by comparing their output with a recorded baseline."
        ),
        heading("1. Introduction"),
        body(
            "Honey bees forage over distances that can exceed several kilometres, which means "
            "that a single rooftop colony samples the flowering plants of an entire neighbourhood. "
            "In a dense city the mix of those plants is shaped by planting schemes, private "
            "gardens, railway verges and the occasional neglected lot. Each of these sources "
            "flowers on its own schedule, and the overlap between schedules decides whether a "
            "colony enjoys a steady supply of nectar or a short glut followed by a long dearth."
        ),
        body(
            "The imagined survey described here asks three questions. First, when does the main "
            "nectar flow begin and end in each year? Second, how much does the timing of that flow "
            "differ between apiaries that are only a few streets apart? Third, can a simple weekly "
            "weighing routine detect the start of a dearth early enough for a beekeeper to respond? "
            "The sections that follow describe the fictional sites, the measurements that would be "
            "taken and the way those measurements could be summarised without specialist software."
        ),
    ])
    footer(c, "Page 1")
    c.showPage()

    render(c, FULL_WIDTH, [
        _block("Helvetica", 9, "Fixture marker RMFX-SC-P2.", space_after=8),
        heading("2. Methods"),
        body(
            "Forty apiaries would be chosen so that every borough of the imagined city contains at "
            "least two of them. Each apiary would hold between two and six colonies kept in standard "
            "hives, and every hive would rest on a platform scale that records its weight once per "
            "hour. The hourly readings would be reduced to a single daily figure by taking the weight "
            "recorded just before dawn, when foragers are inside and the colony is at rest."
        ),
        heading("2.1 Site selection"),
        body(
            "Sites would be grouped by the share of green space within a radius of one kilometre, "
            "estimated from public planning maps rather than from satellite imagery. Low, medium and "
            "high categories would each contain a similar number of apiaries. Rooftops above eight "
            "storeys would be excluded, because strong winds at that height change foraging behaviour "
            "in ways that would be difficult to separate from the effect of the flowering calendar. "
            "Sites closer than three hundred metres to one another would also be excluded, so that "
            "no two apiaries in the survey would routinely share the same nearby flowers."
        ),
        heading("2.2 Measurements"),
        body(
            "Alongside hive weight, each site would record the daily maximum and minimum air "
            "temperature, the number of hours with rain and a weekly count of flowering species within "
            "a fifty metre walk of the hives. The count would follow a fixed route and would note only "
            "whether a species is in flower, not how many flowers are open, because a presence count is "
            "quick, repeatable and easy to teach to volunteers who are not trained botanists."
        ),
        body(
            "A nectar flow would be declared on the first day of a run of at least five days in which "
            "the dawn weight of the median hive rises by more than one hundred grams per day. The flow "
            "would end on the first day of a run of five days without such a rise. These thresholds are "
            "arbitrary, and the document uses them only to have something concrete to describe. A real "
            "study would need to justify them, test their sensitivity and report how often they "
            "disagree with a beekeeper's own judgement of when the flow has started and stopped."
        ),
        body(
            "Weekly summaries would be written as plain tables with one row per apiary and one column "
            "per week, so that the start and end of each flow can be read by eye. Missing days, which "
            "are inevitable when scales lose power or volunteers travel, would be left blank rather than "
            "filled in, and any week with more than two missing days would be marked as incomplete. "
            "Keeping the summaries this simple is a deliberate choice: the purpose of the imagined "
            "survey is to be reproducible by a beekeeping association with a spreadsheet and a few "
            "evenings of volunteer time, not to demonstrate a sophisticated statistical model."
        ),
    ])
    footer(c, "Page 2")
    c.showPage()

    render(c, FULL_WIDTH, [
        _block("Helvetica", 10, "Appendix A. Intentionally brief. RMFX-SC-P3"),
    ])
    footer(c, "Page 3")
    c.showPage()
    c.save()
    return {"pages": 3, "page_markers": {"1": "RMFX-SC-P1", "2": "RMFX-SC-P2", "3": "RMFX-SC-P3"},
            "description": "Single-column paper: long pages that split into several chunks, "
                           "'Page N' footers, and a short final page that indexing skips."}


def build_two_column(path):
    """Two pages laid out in two columns under a full-width title, the layout
    most research papers use. Reading order across columns is part of what
    the golden values capture."""
    gutter = 18
    col_width = (PAGE_WIDTH - 2 * MARGIN - gutter) / 2
    left_x, right_x = MARGIN, MARGIN + col_width + gutter

    c = new_canvas(path, "Pollinator Corridors and Rooftop Gardens")
    title_bottom = render(c, FULL_WIDTH, [
        _block("Helvetica-Bold", 15, "Pollinator Corridors and Rooftop Gardens", space_after=4),
        _block("Helvetica-Oblique", 10, "A two-column layout fixture for PDF extraction tests"),
        _block("Helvetica", 9, NOTICE + " Fixture marker RMFX-TC-P1.", space_after=12),
    ])
    columns = [(left_x, title_bottom, col_width, BODY_BOTTOM), (right_x, title_bottom, col_width, BODY_BOTTOM)]
    render(c, columns, [
        heading("1. Background"),
        body(
            "A pollinator corridor is a chain of flowering patches close enough together that insects "
            "can move along it without crossing long stretches of bare ground. In a city such corridors "
            "are rarely planned as a whole. They emerge from the accidental alignment of gardens, "
            "verges and planted roofs, and they break whenever one link is paved over or replanted "
            "with species that offer little nectar or pollen."
        ),
        body(
            "This fixture imagines a programme that pays building owners a small grant to plant "
            "rooftop gardens chosen to fill gaps in existing corridors. Its text is invented, but it is "
            "written in the register of a short applied paper so that headings, paragraphs and column "
            "breaks resemble what a real document would contain."
        ),
        heading("2. Design"),
        body(
            "Candidate roofs would be ranked by how much each one shortens the longest gap in its local "
            "corridor, using straight-line distances between known flowering patches. The highest "
            "ranked roofs in each district would be offered grants first, and planting lists would favour "
            "species that flower early in spring or late in autumn, when urban forage is scarcest."
        ),
        body(
            "Each funded roof would be visited three times a year by a volunteer who records which "
            "planted species are in flower and counts insect visits during ten minutes of observation "
            "on a calm, dry morning. Visits would be logged on a paper form and transcribed later, so "
            "that no special equipment is needed to take part."
        ),
        heading("3. Observations"),
        body(
            "Because the programme is imaginary, there are no observations to report. The section "
            "exists so that the fixture contains a heading followed by prose that continues into the "
            "next column, which exercises how an extractor orders text that is split across columns "
            "and whether it keeps the end of one column apart from the start of the next."
        ),
    ])
    footer(c, "Synthetic two-column fixture, page 1 of 2")
    c.showPage()

    columns = [(left_x, BODY_TOP, col_width, BODY_BOTTOM), (right_x, BODY_TOP, col_width, BODY_BOTTOM)]
    render(c, columns, [
        _block("Helvetica", 9, "Fixture marker RMFX-TC-P2.", space_after=8),
        heading("4. Discussion"),
        body(
            "Programmes of this kind are attractive because they are cheap and visible, but their "
            "effect is hard to measure. Insects counted on a funded roof might have foraged nearby "
            "anyway, and a roof that fills a gap on a map may still be avoided if it is exposed to "
            "strong wind or shaded by taller buildings for most of the day."
        ),
        body(
            "A fair evaluation would compare funded roofs with similar unfunded ones, follow both for "
            "several years and report the counts in full, including the dull results. It would also ask "
            "building owners whether they kept the planting going after the grant ended, since a garden "
            "that lasts one season contributes little to a corridor."
        ),
        heading("5. Limitations"),
        body(
            "Volunteer counts vary with the observer, the weather and the time of day, and ten minutes "
            "is a short window. Straight-line distance ignores the barriers that insects actually face. "
            "Most importantly, every statement in this document is invented, and it should be read only "
            "as structured filler for software tests."
        ),
        body(
            "The final paragraph is long enough to wrap across several lines of the right-hand column, "
            "so that the last page ends with a column of ordinary prose rather than a heading or a short "
            "fragment, which is the most common way a real paper's body text ends before its references."
        ),
    ])
    footer(c, "Synthetic two-column fixture, page 2 of 2")
    c.showPage()
    c.save()
    return {"pages": 2, "page_markers": {"1": "RMFX-TC-P1", "2": "RMFX-TC-P2"},
            "description": "Two-column paper under a full-width title: reading order across columns."}


def build_typography(path):
    """Three pages: Windows-1252 punctuation, accents and symbols, a hyphen
    split across lines and a small table drawn cell by cell; a blank page; and
    a monospaced block with runs of spaces that clean_text collapses."""
    c = new_canvas(path, "Typography and Encoding Fixture")
    y = render(c, FULL_WIDTH, [
        _block("Helvetica-Bold", 14, "Typography and Encoding Fixture", space_after=4),
        _block("Helvetica", 9, NOTICE + " Fixture marker RMFX-TY-P1.", space_after=10),
        heading("Accents, quotation marks and dashes"),
        body(
            "The fictional reviewers Zoë Ångström, Jürgen Weiß, François Lefèvre and Señora Muñoz "
            "described the “main flow” as a ‘short but generous’ period – roughly two weeks – that "
            "began after a cool spell… They noted temperatures near 25 °C, weight changes of ±0.4 kg, "
            "a 3 × 4 grid of sample frames, pollen grains of about 30 µm and a feeder budget of €12."
        ),
        body(
            "• Bullet one: early willow and hazel. • Bullet two: lime trees in midsummer. "
            "• Bullet three: ivy in late autumn — the last reliable source before winter."
        ),
    ])
    c.setFont("Times-Roman", 10.5)
    y -= 6
    c.drawString(MARGIN, y - 13.5, "The word inter-")
    c.drawString(MARGIN, y - 27, "national is split across two lines with a hyphen.")
    y -= 45
    c.setFont("Helvetica-Bold", 9)
    columns = [MARGIN, MARGIN + 120, MARGIN + 220, MARGIN + 340]
    for x, label in zip(columns, ["Site", "Week", "Dawn weight (kg)", "Change"]):
        c.drawString(x, y, label)
    c.setFont("Helvetica", 9)
    rows = [("North roof", "12", "41.2", "+0.6"), ("Canal wharf", "12", "38.9", "-0.2"),
            ("Market hall", "13", "44.0", "+1.1"), ("Old depot", "13", "36.5", "0.0")]
    for row in rows:
        y -= 13
        for x, cell in zip(columns, row):
            c.drawString(x, y, cell)
    c.showPage()

    # Page 2 is deliberately blank: no drawing operations at all.
    c.showPage()

    render(c, FULL_WIDTH, [
        _block("Helvetica", 9, "Fixture marker RMFX-TY-P3.", space_after=8),
        heading("Monospaced block"),
    ])
    c.setFont("Courier", 9)
    code_lines = [
        "weights    =   [41.2,  38.9,   44.0,    36.5]",
        "threshold  =   0.1      # kilograms per day",
        "flow_days  =   count_runs(weights,  threshold,   min_length=5)",
        "report(flow_days)          # a run of spaces before this comment",
    ]
    y = BODY_TOP - 60
    for line in code_lines:
        c.drawString(MARGIN, y, line)
        y -= 12
    y -= 12
    render(c, [(MARGIN, y, PAGE_WIDTH - 2 * MARGIN, BODY_BOTTOM)], [
        body(
            "The block above uses runs of spaces to align its columns. Text extraction keeps those "
            "spaces, and the cleaning step that runs before chunking collapses each run to a single "
            "space, so the cleaned text differs from the extracted text on this page by design."
        ),
        body(
            "1. Weigh every hive before dawn. 2. Note the temperature. 3. Walk the fixed route and "
            "record which species are in flower. 4. Leave missing days blank rather than guessing."
        ),
    ])
    c.showPage()
    c.save()
    return {"pages": 3, "page_markers": {"1": "RMFX-TY-P1", "3": "RMFX-TY-P3"}, "blank_pages": [2],
            "description": "Windows-1252 punctuation and symbols, a hyphenated line break, a small "
                           "table, a blank page, and a monospaced block with runs of spaces."}


BUILDERS = {
    "single_column.pdf": build_single_column,
    "two_column.pdf": build_two_column,
    "typography.pdf": build_typography,
}


def generate(out_dir):
    entries = {}
    for name, build in BUILDERS.items():
        path = out_dir / name
        info = build(path)
        data = path.read_bytes()
        entries[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), **info}
    return entries


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="regenerate in a temporary directory and compare with manifest.json")
    args = parser.parse_args(argv)

    if args.check:
        recorded = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))["fixtures"]
        with tempfile.TemporaryDirectory() as tmp:
            fresh = generate(Path(tmp))
        mismatched = [n for n in BUILDERS if fresh[n]["sha256"] != recorded.get(n, {}).get("sha256")]
        if mismatched:
            print("Regenerated bytes differ from manifest.json for: " + ", ".join(mismatched))
            return 1
        print(f"All {len(BUILDERS)} fixtures reproduce byte for byte with reportlab {reportlab.Version}.")
        return 0

    entries = generate(HERE)
    manifest = {
        "about": "Synthetic PDF fixtures; see README.md for licence and provenance.",
        "generator": "tests/fixtures/pdf/generate_fixtures.py",
        "generated_with": {"reportlab": reportlab.Version},
        "fixtures": entries,
    }
    (HERE / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {len(entries)} fixtures and manifest.json with reportlab {reportlab.Version}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
