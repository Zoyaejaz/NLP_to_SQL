"""Upload cleaning / type-inference tests – no database needed.   Run: pytest -q   (from ./backend)"""
import io

import pandas as pd
import pytest

from app.uploads import UploadError, _read_csv, clean_columns, clean_table_name, prepare_frame


def test_column_names_are_clean_unique_and_safe():
    cols = clean_columns(["Order ID", "Amount (₹)", "orderDate", "Unnamed: 3", "order", "Order ID", "2024 sales", ""])
    assert cols == ["order_id", "amount", "order_date", "column_4", "order_", "order_id_2", "c_2024_sales", "column_8"]


@pytest.mark.parametrize("raw,expected", [
    ("Sales Report 2024", "sales_report_2024"),
    ("2024 data", "t_2024_data"),
    ("app", "app_data"),
    ("order", "order_data"),
    ("pg_class", "t_pg_class"),
    ('x"; DROP TABLE customers;--', "x_drop_table_customers"),
    ("!!!", "my_data"),
])
def test_table_names(raw, expected):
    assert clean_table_name(raw) == expected


def _frame(csv_text: str, state=None):
    df = _read_csv(csv_text.encode(), ".csv")
    frame, types, notes = prepare_frame(df, state)
    return frame, {c: type(t).__name__.lower() for c, t in types.items()}, notes


def test_leading_zeros_and_long_ids_stay_text():
    _, t, _ = _frame("zip,card\n00501,1234567890123456\n01234,6543210987654321\n")
    assert t == {"zip": "text", "card": "text"}


def test_money_and_percent_become_numbers():
    f, t, _ = _frame('amt,disc\n"₹1,200.50",10%\n"₹2,000",5%\n')
    assert t["amt"] == "float" and t["disc"] == "biginteger"
    assert list(f["amt"]) == [1200.5, 2000.0]


def test_indian_and_western_grouping():
    f, t, _ = _frame('a,b\n"12,34,567","1,234,567"\n"1,00,000","2,000"\n')
    assert t == {"a": "biginteger", "b": "biginteger"}
    assert sorted(f["a"]) == [100000, 1234567]


def test_decimal_comma_is_not_corrupted():
    f, t, _ = _frame('price\n"2,5"\n"3"\n')
    assert t["price"] == "text" and list(f["price"]) == ["2,5", "3"]


def test_dates_dayfirst_and_iso():
    state = {}
    f, t, _ = _frame("d\n13/04/2024\n03/04/2024\n", state)
    assert t["d"] == "date" and str(f["d"][0]) == "2024-04-13" and str(f["d"][1]) == "2024-04-03"
    assert state.get("dayfirst") is True          # handle_upload turns this into a note for the user
    state = {}
    f, t, _ = _frame("d\n2024-04-13\n2024-05-01 10:30:00\n", state)
    assert t["d"] == "datetime" and not state


def test_booleans_and_blank_rows():
    f, t, _ = _frame("ok,n\nTrue,1\nfalse,2\n,\nTRUE,3\n")
    assert t["ok"] == "boolean" and t["n"] == "biginteger" and len(f) == 3


def test_mixed_column_stays_text():
    _, t, _ = _frame("v\n12\nabc\n7\n")
    assert t["v"] == "text"


def test_semicolon_and_latin1_csv():
    df = _read_csv("nom;prix\nCafé;2\nThé;3\n".encode("latin-1"), ".csv")
    assert list(df.columns) == ["nom", "prix"] and df["nom"][0] == "Café"


def test_pipe_and_tab_delimiters():
    assert list(_read_csv(b"a|b\n1|2\n", ".csv").columns) == ["a", "b"]
    assert list(_read_csv(b"a\tb\n1\t2\n", ".tsv").columns) == ["a", "b"]


def test_ragged_csv_is_a_clean_error():
    with pytest.raises(UploadError, match="Line 2"):
        _read_csv(b"a,b\n1,2,3,4\n5\n", ".csv")
    with pytest.raises(UploadError):
        _read_csv(b"a,b\n1,2\n3,4,5\n", ".csv")


def test_trailing_commas_do_not_shift_values():
    f, _, _ = _frame("a,b\n1,2,\n3,4,\n")
    assert f.values.tolist() == [[1, 2], [3, 4]]
