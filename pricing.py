STYLE_TIERS = {
    "functional": {
        "label": "Functional & Beautiful",
        "tagline": "Smart, simple, practical, great value",
        "rate_per_sqft": 950,
    },
    "elegant": {
        "label": "Elegant Everyday Living",
        "tagline": "Premium, elegant, balanced finishes",
        "rate_per_sqft": 1400,
    },
    "refined": {
        "label": "Refined Designer Home",
        "tagline": "Sophisticated, warm, rich detailing",
        "rate_per_sqft": 2100,
    },
    "signature": {
        "label": "One-of-a-Kind Dream Home",
        "tagline": "Signature, luxurious, completely bespoke",
        "rate_per_sqft": 3200,
    },
}


BUDGET_RANGES = {
    "under_10l": {
        "label": "Under ₹10L",
        "min": 0,
        "max": 1000000,
    },
    "10l_15l": {
        "label": "₹10L–₹15L",
        "min": 1000000,
        "max": 1500000,
    },
    "15l_20l": {
        "label": "₹15L–₹20L",
        "min": 1500000,
        "max": 2000000,
    },
    "20l_30l": {
        "label": "₹20L–₹30L",
        "min": 2000000,
        "max": 3000000,
    },
    "above_30l": {
        "label": "More than ₹30L",
        "min": 3000000,
        "max": None,
    },
    "not_sure": {
        "label": "Not sure yet",
        "min": None,
        "max": None,
    },
}


DEFAULT_SCOPE_ITEMS = [
    "Room-wise modular furniture scope",
    "Kitchen cabinets",
    "Wardrobes",
    "TV units",
    "Vanities",
    "Storage units",
]


def estimate_cost(area_sqft: float, style_key: str) -> dict:
    tier = STYLE_TIERS[style_key]

    estimated_investment = area_sqft * tier["rate_per_sqft"]
    estimated_investment_lakhs = round(estimated_investment / 100000, 2)

    return {
        "area_sqft": area_sqft,
        "style_key": style_key,
        "style_label": tier["label"],
        "rate_per_sqft": tier["rate_per_sqft"],
        "scope": DEFAULT_SCOPE_ITEMS,
        "estimated_investment": estimated_investment,
        "estimated_investment_lakhs": estimated_investment_lakhs,
        "confidence": 88,
    }


def compare_to_budget(estimate_amount: float, budget_key: str) -> str:
    budget = BUDGET_RANGES[budget_key]

    budget_min = budget["min"]
    budget_max = budget["max"]

    if budget_min is None and budget_max is None:
        return "unknown"

    tolerance = 0.05

    if budget_max is not None and estimate_amount > budget_max * (1 + tolerance):
        return "over"

    if budget_min is not None and estimate_amount < budget_min * (1 - tolerance):
        return "under"

    return "match"