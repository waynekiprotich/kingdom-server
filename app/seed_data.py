"""Development seed data.

**This is not real catalog content.** It is structured stand-in data so the
storefront can be built and tested against realistic shapes — price ranges,
size runs, sold-out variants, multi-image galleries — before the business
supplies actual products and photography.

Images point at Cloudinary's public ``demo`` cloud so the real delivery
pipeline (transformations, ``srcset``, ``f_auto``) is genuinely exercised.
Replace the cloud name and public ids when real photography is uploaded.

Prices are in KES and chosen to be plausible for a premium local label; they
are not the business's prices.
"""

from __future__ import annotations

from decimal import Decimal

CATEGORIES = [
    {"name": "Shirts", "slug": "shirts", "position": 1,
     "description": "Everyday shirts, cut for the Nairobi climate."},
    {"name": "Dresses", "slug": "dresses", "position": 2,
     "description": "Day-to-evening pieces."},
    {"name": "Trousers", "slug": "trousers", "position": 3,
     "description": "Tailored and relaxed cuts."},
    {"name": "Outerwear", "slug": "outerwear", "position": 4,
     "description": "Layers for the cold months."},
    {"name": "Accessories", "slug": "accessories", "position": 5,
     "description": "Bags and finishing pieces."},
]

SIZES = ("XS", "S", "M", "L", "XL")

PRODUCTS = [
    {
        "name": "Kitenge Camp Collar Shirt",
        "slug": "kitenge-camp-collar-shirt",
        "category": "shirts",
        "description": (
            "A relaxed camp collar shirt in hand-picked kitenge. Cut straight "
            "through the body with a single chest pocket and a soft, unlined "
            "collar that sits flat open or closed."
        ),
        "base_price": Decimal("3800.00"),
        "featured": True,
        "images": ["sample", "couple"],
        "colors": {"Indigo": 6, "Rust": 4},
    },
    {
        "name": "Linen Overshirt",
        "slug": "linen-overshirt",
        "category": "shirts",
        "description": (
            "Mid-weight linen with a boxy shoulder, worn open over a tee or "
            "buttoned as a light jacket. Softens with every wash."
        ),
        "base_price": Decimal("4500.00"),
        "sale_price": Decimal("3600.00"),
        "featured": True,
        "images": ["woman"],
        "colors": {"Bone": 5, "Olive": 3},
    },
    {
        "name": "Poplin Shirt Dress",
        "slug": "poplin-shirt-dress",
        "category": "dresses",
        "description": (
            "A full-length shirt dress in crisp cotton poplin, with a "
            "removable belt and deep side pockets."
        ),
        "base_price": Decimal("6200.00"),
        "featured": True,
        "images": ["woman", "sample", "couple"],
        "colors": {"Black": 4, "Sand": 2},
    },
    {
        "name": "Ankara Wrap Dress",
        "slug": "ankara-wrap-dress",
        "category": "dresses",
        "description": (
            "A true wrap in ankara cotton, adjustable at the waist, falling "
            "just below the knee."
        ),
        "base_price": Decimal("5800.00"),
        "images": ["woman"],
        "colors": {"Emerald": 3},
    },
    {
        "name": "Pleated Wide Leg Trouser",
        "slug": "pleated-wide-leg-trouser",
        "category": "trousers",
        "description": (
            "Double-pleated and high-waisted, in a fluid twill that holds a "
            "line without pressing."
        ),
        "base_price": Decimal("5200.00"),
        "images": ["couple"],
        "colors": {"Charcoal": 5, "Stone": 4},
    },
    {
        "name": "Drawstring Linen Trouser",
        "slug": "drawstring-linen-trouser",
        "category": "trousers",
        "description": "An easy tapered trouser with a covered drawstring waist.",
        "base_price": Decimal("4200.00"),
        "images": ["sample"],
        "colors": {"Ecru": 0, "Navy": 0},  # sold out — exercises the empty state
    },
    {
        "name": "Quilted Bomber",
        "slug": "quilted-bomber",
        "category": "outerwear",
        "description": (
            "A light quilted bomber with ribbed cuffs and a two-way zip. Warm "
            "enough for a July morning in the highlands."
        ),
        "base_price": Decimal("9800.00"),
        "featured": True,
        "images": ["couple", "sample"],
        "colors": {"Black": 3, "Forest": 2},
    },
    {
        "name": "Canvas Field Jacket",
        "slug": "canvas-field-jacket",
        "category": "outerwear",
        "description": "Four-pocket cotton canvas, cut long, unlined.",
        "base_price": Decimal("11500.00"),
        "sale_price": Decimal("8900.00"),
        "images": ["sample"],
        "colors": {"Khaki": 2},
    },
    {
        "name": "Leather Market Tote",
        "slug": "leather-market-tote",
        "category": "accessories",
        "description": (
            "A single-piece leather tote with saddle-stitched handles. No "
            "lining, no hardware — it softens into shape with use."
        ),
        "base_price": Decimal("7400.00"),
        "images": ["leather_bag_gray"],
        "sizes": ("One size",),
        "colors": {"Grey": 4, "Tan": 3},
    },
    {
        "name": "Woven Leather Belt",
        "slug": "woven-leather-belt",
        "category": "accessories",
        "description": "A woven leather belt with a brushed brass buckle.",
        "base_price": Decimal("2600.00"),
        "images": ["shoes"],
        "sizes": ("S", "M", "L"),
        "colors": {"Brown": 6, "Black": 5},
    },
]
