"""The seven jerky recipes plus the vinegar bath, as supplied.

Quantities are GRAMS PER 10 KG OF MEAT, exactly as written on the sheets —
spellings included, at Tarang's instruction. An ingredient listed twice is two
separate weighings at different stages, so the second is suffixed to keep them
distinguishable on the operator's screen.
"""

# name -> (meat or None, [(ingredient, grams per 10 kg), ...])
RECIPES = {
"Vinegar Bath": (None, [
    ("White vinegar", 150.00), ("Papain", 20.00)]),

"Teriyaki Jerky": (None, [
    ("teriyaki sauce", 730.00), ("soy sauce", 464.00), ("honey", 420.00),
    ("Tamarind pulp", 254.00), ("White vinegar", 254.00), ("verdad", 20.00),
    ("jaggery powder", 232.00), ("sea salt", 75.00), ("seasame seed", 45.00),
    ("Yeast extract", 10.00), ("black pepper", 11.00), ("Onion powder", 14.00),
    ("chilli flake", 13.00), ("garlic powder", 6.00), ("bhut jholokia", 2.50),
    ("liquid smoke", 0.00)]),

"Gochujangh Jerky": (None, [
    ("soy sauce", 1275.00), ("gochujang paste", 1000.00),
    ("jaggery powder", 400.00), ("onion powder", 200.00), ("Verdad", 20.00),
    ("garlic powder", 80.00), ("chilli flakes powder", 60.00),
    ("White vinegar", 60.00), ("sesame seeds", 50.00), ("Yeast Extract", 10.00),
    ("bhut jholokia powder", 8.00)]),

"Pepper Jerky": (None, [
    ("onion powder", 12.28), ("garlic powder", 21.48), ("black pepper", 141.08),
    ("jaggery powder", 452.86), ("soy sauce", 1613.44), ("paprika", 61.45),
    ("yeast extract", 10.00), ("Verdad N6", 20.00), ("liquid smoke", 0.00)]),

"Karnatka Nati Jerky": ("Country chicken", [
    ("coconut milk", 1800.00), ("curry leaf powder", 9.50),
    ("fennel powder", 120.00), ("black pepper", 72.00),
    ("onion powder", 360.00), ("ginger powder", 120.00),
    ("garlic powder", 240.00), ("soya sauce", 2019.00),
    ("green chilli powder", 150.00), ("vedad", 20.00),
    ("yeast extract", 10.00), ("tamarind paste", 450.00),
    ("olive oil", 388.70), ("dessicated coconut", 68.02),
    ("curry leaf powder (2nd addition)", 10.37)]),

"Kerala fry Jerky": (None, [
    ("coconut milk", 1505.75), ("curry leaf powder", 18.39),
    ("cumin powder", 50.00), ("fennel powder", 100.00),
    ("black pepper", 30.00), ("garam masala", 41.38),
    ("corriender powder", 41.38), ("onion powder", 300.00),
    ("ginger powder", 100.00), ("garlic powder", 200.00),
    ("soya sauce", 1685.82), ("green chilli powder", 100.00),
    ("Verdad N6", 20.00), ("yeast extract", 10.00), ("olive oil", 388.70),
    ("dessicated coconut", 68.02),
    ("curry leaf powder (2nd addition)", 10.37)]),

"Mughlai Jerky": (None, [
    ("Curd", 2000.00), ("Cashew", 750.00), ("Honey", 200.00), ("Salt", 150.00),
    ("Kasuri meethi", 20.00), ("Cardamom", 16.00), ("Garam Masala", 20.00),
    ("Jeera Poawder", 20.00), ("Jaggery Powder", 150.00), ("Green Chili", 25.00),
    ("Yeast Extract", 10.00), ("Verdad", 20.00)]),

"Masala Jerky": ("Chicken", [
    ("ONION POWDER", 42.77), ("GARLIC POWDER", 39.96),
    ("CRACKED BLACK PEPPER", 25.35), ("JAGGERY POWDER", 436.12),
    ("DARK SOY SAUCE", 807.43), ("TERIYAKI SAUCE", 420.55),
    ("BALSAMIC VINEGAR", 457.55), ("PINEAPPLE JUICE", 782.51),
    ("BHUT JHOLOKIA POWDER", 6.00), ("verdad", 20.00), ("Yeast extract", 10.00),
    ("SALT", 45.50), ("CHILLI FLAKES", 21.60),
    ("CRACKED BLACK PEPPER (2nd addition)", 23.00), ("THYME", 13.20),
    ("OREGENO FLAKES", 12.00), ("RAW GARLIC", 140.00), ("RAW GINGER", 200.00),
    ("OLIVE OIL", 665.00)]),
}

BATCH_G = 10000.0     # the basis every quantity above is written against


def as_percent(grams_per_10kg):
    """g per 10 kg of meat -> percent of the meat weight."""
    return round(grams_per_10kg / BATCH_G * 100, 6)
