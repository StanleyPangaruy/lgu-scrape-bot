"""Excel downloads.

Comment text is written strictly as text: a comment beginning with "=" would
otherwise become a live formula in Excel (formula injection), and control
characters that Excel rejects are removed.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FORMULA_START = ("=", "+", "-", "@")


def _cell_value(value: Any, tz: timezone) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(tz).replace(tzinfo=None)
    if isinstance(value, str):
        value = ILLEGAL_CHARACTERS_RE.sub("", value)
    return value


def add_sheet(
    wb: Workbook,
    title: str,
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    tz: timezone,
    widths: Sequence[int] | None = None,
) -> None:
    ws = wb.create_sheet(title=title[:31])
    ws.append(list(headers))
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"
    for row in rows:
        ws.append([_cell_value(v, tz) for v in row])
        for cell in ws[ws.max_row]:
            if isinstance(cell.value, str):
                # openpyxl turns strings starting with "=" into formulas; force text.
                cell.data_type = "s"
                if cell.value.startswith(FORMULA_START):
                    cell.quotePrefix = True
            elif isinstance(cell.value, datetime):
                cell.number_format = "yyyy-mm-dd hh:mm"
    for i, width in enumerate(widths or [], 1):
        ws.column_dimensions[get_column_letter(i)].width = width


def new_workbook() -> Workbook:
    wb = Workbook()
    wb.remove(wb.active)
    return wb


def to_bytes(wb: Workbook) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def from_timestamp(ts: int | None) -> datetime | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
