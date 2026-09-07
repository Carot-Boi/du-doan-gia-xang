"""
Product code lookup tables.

MOIT adds/retires priced products over time (policy changes noted in bulletin
prose — e.g. E10RON95-III replacing RON95-III as the reference gasoline from
16/6/2026, kerosene dropped as a state-priced product from 29/4/2026). Keeping
product identity as data here (not scattered string literals in parsing/
validation logic) means a future product change is a one-line edit, not a
code change hunting through regexes.

Two separate code spaces are used because the WORLD reference-price table
(Singapore platform quotes) and the VIETNAM retail/BOG products are not a
1:1 mapping (e.g. one world "RON95" quote underlies the E10RON95-III retail
price; FO/mazut is quoted in USD/ton globally but sold in VND/kg locally).
"""

# World reference-price columns, as they appear in the "Giá thành phẩm xăng
# dầu thế giới" daily table published in every bulletin.
WORLD_PRODUCTS = {
    "RON92": {"label": "X92", "unit": "USD/thung", "description": "Xăng RON92 (dùng pha chế E5RON92)"},
    "RON95": {"label": "X95", "unit": "USD/thung", "description": "Xăng RON95 (dùng pha chế E10RON95-III / trước đây RON95-III)"},
    "KEROSENE": {"label": "Dầu hoả", "unit": "USD/thung", "description": "Dầu hỏa — ngừng là mặt hàng nhà nước công bố giá cơ sở từ 29/4/2026 (Thông tư 21/2026/TT-BCT)"},
    "DIESEL_0_05S": {"label": "DO 0,05", "unit": "USD/thung", "description": "Dầu điêzen 0,05S"},
    "FO_180CST_3_5S": {"label": "FO 3,5S", "unit": "USD/tan", "description": "Dầu mazut 180CST 3,5S"},
    "VCB_BUY": {"label": "VCB mua CK", "unit": "VND/USD", "description": "Tỷ giá Vietcombank mua chuyển khoản"},
    "VCB_SELL": {"label": "VCB bán", "unit": "VND/USD", "description": "Tỷ giá Vietcombank bán"},
}

# Order matters: this is the left-to-right column order of the table, used
# to map cell position -> product code when parsing <tr><td> sequences.
WORLD_TABLE_COLUMN_ORDER = [
    "RON92", "RON95", "KEROSENE", "DIESEL_0_05S", "FO_180CST_3_5S", "VCB_BUY", "VCB_SELL",
]

# Retail / cycle-summary / BOG products. "aliases" lists the different
# prose spellings MOIT has used for the same product over time, so the
# parser can match any of them without hardcoding assumptions about which
# name is "current". RON95-III and E10RON95-III are kept as separate codes
# (rather than silently merged) because they are, formally, different
# reference products under different policy regimes — but both map to the
# same underlying world RON95 quote via `world_reference`.
RETAIL_PRODUCTS = {
    "E5RON92": {
        "aliases": ["Xăng E5RON92", "E5RON92"],
        "unit": "VND/lit",
        "world_reference": "RON92",
    },
    "RON95III": {
        "aliases": ["Xăng RON95-III", "RON95-III"],
        "unit": "VND/lit",
        "world_reference": "RON95",
    },
    "E10RON95III": {
        "aliases": ["Xăng E10RON95-III", "E10RON95-III"],
        "unit": "VND/lit",
        "world_reference": "RON95",
    },
    "KEROSENE": {
        "aliases": ["Dầu hỏa", "Dầu hoả"],
        "unit": "VND/lit",
        "world_reference": "KEROSENE",
    },
    "DIESEL_0_05S": {
        "aliases": ["Dầu điêzen 0.05S", "Dầu điêzen 0,05S", "Dầu diesel 0.05S"],
        "unit": "VND/lit",
        "world_reference": "DIESEL_0_05S",
    },
    "FO_180CST_3_5S": {
        "aliases": ["Dầu madút 180CST 3.5S", "Dầu madút 180CST 3,5S", "Dầu mazut 180CST 3.5S"],
        "unit": "VND/kg",
        "world_reference": "FO_180CST_3_5S",
    },
}

# BOG (Quỹ Bình ổn giá) section uses yet another set of short labels
# ("Xăng sinh học" = biofuel-blended gasoline collectively, not split by
# E5/E10) — kept distinct rather than force-mapped onto RETAIL_PRODUCTS.
BOG_PRODUCTS = {
    "XANG_SINH_HOC": {"aliases": ["Xăng sinh học"], "unit": "VND/lit"},
    # "Xăng không chì" (unleaded) is an older BOG line item name seen in
    # bulletins predating the E10 biofuel-blend mandate — kept as its own
    # code rather than merged into XANG_SINH_HOC, since the two names were
    # never simultaneously in use for the same product (policy renamed it,
    # didn't add a second product), and conflating them would silently
    # combine two different eras' BOG figures under one code.
    "XANG_KHONG_CHI": {"aliases": ["Xăng không chì"], "unit": "VND/lit"},
    "DIESEL_0_05S": {"aliases": ["Dầu điêzen", "Dầu diesel"], "unit": "VND/lit"},
    "FO_180CST_3_5S": {"aliases": ["Dầu madút", "Dầu mazut"], "unit": "VND/kg"},
}


def world_label_to_code(label: str) -> str | None:
    """Map a table header cell (e.g. 'X92', 'DO 0,05') to a WORLD_PRODUCTS code."""
    label_norm = label.strip()
    for code, info in WORLD_PRODUCTS.items():
        if info["label"] == label_norm:
            return code
    return None
