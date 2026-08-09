"""PDFs built to break the extractor, generated rather than committed.

Every file here is produced from code, so what makes each one hard is written down
instead of hidden inside a binary somebody dropped in the repo years ago, and a case can
be adjusted by editing the thing that describes it.

Each builder returns PDF bytes and is named for the single property under test.
"""

from __future__ import annotations

import pymupdf

# The clauses the corpus hides inside awkward layouts. Extraction has to surface these
# whatever the page does, or say it could not -- never quietly return the page without.
PAYMENT_CLAUSE = (
    "4.3 Undisputed invoice amounts are due thirty (30) calendar days after Customer's "
    "receipt of a correct invoice."
)
LIABILITY_CLAUSE = (
    "10.1 Aggregate liability will not exceed two hundred fifty thousand U.S. dollars "
    "(USD 250,000)."
)
NOTICE_CLAUSE = (
    "9.2 Either party may terminate this Agreement for convenience on sixty (60) days' "
    "prior written notice."
)


def _page(document: pymupdf.Document, *, width: float = 595, height: float = 842):
    return document.new_page(width=width, height=height)


def readable_pdf() -> bytes:
    """The control: an ordinary single-column page nothing is wrong with."""
    document = pymupdf.open()
    page = _page(document)
    page.insert_textbox(
        pymupdf.Rect(50, 50, 545, 500),
        f"MASTER SERVICES AGREEMENT\n\n{PAYMENT_CLAUSE}\n\n{LIABILITY_CLAUSE}\n\n{NOTICE_CLAUSE}",
        fontsize=11,
    )
    return document.tobytes()


def rotated_pdf(rotation: int = 90) -> bytes:
    """Real text, page rotated. The glyphs are all there; the reading order is the risk."""
    document = pymupdf.open()
    page = _page(document)
    page.insert_textbox(
        pymupdf.Rect(50, 50, 545, 500),
        f"MASTER SERVICES AGREEMENT\n\n{PAYMENT_CLAUSE}\n\n{LIABILITY_CLAUSE}",
        fontsize=11,
    )
    page.set_rotation(rotation)
    return document.tobytes()


def scanned_pdf() -> bytes:
    """A picture of a page: no text layer at all."""
    document = pymupdf.open()
    page = _page(document)
    page.draw_rect(page.rect, color=(0.1, 0.1, 0.1), fill=(0.85, 0.85, 0.85))
    return document.tobytes()


def multi_column_pdf() -> bytes:
    """Two columns, each a complete clause.

    Ordering by vertical position alone interleaves them: the first line of the right
    column sits level with the first line of the left, so the payment clause comes out
    spliced through the liability clause and neither can be quoted.
    """
    document = pymupdf.open()
    page = _page(document)
    left = f"LEFT COLUMN\n\n{PAYMENT_CLAUSE}\n\nThis column continues down the page."
    right = f"RIGHT COLUMN\n\n{LIABILITY_CLAUSE}\n\nThis column also continues downward."
    page.insert_textbox(pymupdf.Rect(40, 60, 280, 700), left, fontsize=10)
    page.insert_textbox(pymupdf.Rect(320, 60, 560, 700), right, fontsize=10)
    return document.tobytes()


def table_heavy_pdf() -> bytes:
    """A ruled fee schedule: mostly digits, separators and currency.

    Letter density says "mojibake" about a page like this, so a naive garble check sends
    a perfectly legible table to OCR, or fails the upload over it.
    """
    document = pymupdf.open()
    page = _page(document)
    page.insert_text((50, 50), "SCHEDULE 2 - FEES", fontsize=12)
    rows = [
        ("Item", "Qty", "Unit (USD)", "Amount (USD)"),
        ("Managed Kubernetes", "1", "12,000.00", "12,000.00"),
        ("Platform support", "2", "2,250.00", "4,500.00"),
        ("On-call retainer", "1", "2,000.00", "2,000.00"),
        ("TOTAL DUE", "", "", "18,500.00"),
    ]
    top, left, row_height, widths = 80, 50, 24, (200, 60, 110, 120)
    for index, row in enumerate(rows):
        y = top + index * row_height
        x = left
        for width, cell in zip(widths, row, strict=True):
            page.draw_rect(pymupdf.Rect(x, y, x + width, y + row_height), color=(0, 0, 0))
            page.insert_text((x + 4, y + 16), cell, fontsize=9)
            x += width
    return document.tobytes()


def encrypted_pdf(*, user_password: str = "", owner_password: str = "owner") -> bytes:
    """Owner-locked by default, which an empty user password opens; give a
    `user_password` to make it genuinely unreadable without one."""
    document = pymupdf.open(stream=readable_pdf(), filetype="pdf")
    return document.tobytes(
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw=owner_password,
        user_pw=user_password,
        permissions=pymupdf.PDF_PERM_ACCESSIBILITY,
    )


def corrupt_pdf() -> bytes:
    """A header and then garbage: the shape of a truncated upload."""
    return b"%PDF-1.7\n" + bytes(range(256)) * 8


def truncated_pdf() -> bytes:
    """A real PDF cut off halfway, losing its cross-reference table."""
    return readable_pdf()[: len(readable_pdf()) // 2]


def empty_page_pdf() -> bytes:
    """A structurally valid PDF whose page carries nothing at all."""
    document = pymupdf.open()
    _page(document)
    return document.tobytes()


CORPUS = {
    "readable": readable_pdf,
    "rotated": rotated_pdf,
    "scanned": scanned_pdf,
    "multi_column": multi_column_pdf,
    "table_heavy": table_heavy_pdf,
    "encrypted": encrypted_pdf,
    "corrupt": corrupt_pdf,
    "truncated": truncated_pdf,
    "empty_page": empty_page_pdf,
}
