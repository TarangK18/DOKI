# Review — `Recipes for Yield calculation - Teriyaki.csv`

Checked against the sheet's own arithmetic and against the weighing station's
5 g scale.

## Every formula computes correctly

I recomputed all 16 marinade lines, both product-cost blocks and the pack
block from the raw inputs. Every figure matches to the last decimal:

| checked | sheet | recomputed |
|---|---|---|
| marinade batch weight | 2550.5 g | 2550.5 g |
| marinade cost in use | 1029.74626 | 1029.74626 |
| marinade cost per kg | 403.7428975 | 403.7428975 |
| marinade per 1000 g meat | 255.05 g | 255.05 g |
| chicken product cost | 561.1424831 | 561.142483 |
| buff product cost | 854.749626 | 854.749626 |
| chicken cost per pack | 33.45272496 | 33.45272496 |
| buff cost per pack | 50.9562277 | 50.95622771 |

So there is no spreadsheet error. The problems below are modelling problems —
the formulas faithfully compute something that isn't quite the right thing.

---

## 1. Dehydration loss is counted twice — cost per pack is ~78 % too high

This is the significant one.

The sheet applies the yield loss in **two** places:

- **In the meat cost.** Chicken is costed as `320 × 1000 g × 1.428571`, where
  1.428571 is 1/0.70. Multiplying by 1/yield only makes sense if the 1000 g is
  *finished* product and you are grossing up to the raw meat needed.
- **In the pack count.** The pack block then takes `1000 g × 52 % = 520 g` and
  divides by the pack size. Multiplying by the yield only makes sense if the
  1000 g is *raw* meat being shrunk down.

Both cannot be true of the same 1000 g. As written, the numerator is the cost
of 1000 g of finished jerky and the denominator is the pack count from 520 g of
finished jerky, so the shrinkage is charged twice.

The two self-consistent readings:

| | chicken | buff |
|---|---|---|
| **sheet as written** | ₹33.45 | ₹50.96 |
| if 1000 g means *finished* weight (32.26 packs) | ₹17.40 | ₹26.50 |
| if 1000 g means *raw* meat, no conversion factor (22.58 / 19.35 packs) | ₹18.78 | ₹28.66 |

Whichever basis is intended, the sheet overstates cost per pack by about 78 %.
Worth settling before this number reaches a price list.

## 2. Three different yields in one sheet

- Yield table: chicken **70 %**, buff **60 %**
- Conversion factors: **1.4286** (= 1/0.70) and **1.6667** (= 1/0.60), consistent
  with the table
- Pack block: **52 %** for both meats

52 % appears nowhere else and contradicts both the table and the factors, and
it is applied identically to chicken and buff even though the table says they
dry differently. If 52 % is the real measured yield, the conversion factors are
wrong; if 70/60 % is real, the pack block is wrong. They cannot both stand.

## 3. Pack size is invisible

Cost per pack divides by 16.77419355, which is `520 / 31`. The **31 g** pack
size exists only inside that formula — it is not a labelled cell anywhere. Any
change to pack size means editing a magic number by hand in two places. Give it
its own labelled input.

## 4. Liquid smoke is listed with a quantity of 0

Rate ₹1050/kg, quantity `0.00`, contributing ₹0. Either it was dropped from the
recipe and left in the sheet, or a number is missing.

## 5. Two labelling slips

- **"Marinade per Gm of meat 255.05"** — that is per *kilogram* of meat, not
  per gram. The arithmetic downstream uses it correctly as g/kg, so only the
  label is wrong.
- **"Rate 403.7"** for marinade in the product blocks displays rounded but
  computes on the full 403.7428975. Harmless, but the displayed figure doesn't
  reconcile if anyone checks by hand.

## 6. `Con F (Waste)` is 1 for every marinade ingredient

Waste isn't modelled for anything in the marinade — no trim, spill or
carry-over loss on sticky items like honey and tamarind pulp, or dusty ones
like the spice powders. If that's deliberate, fine; if the column was meant to
be filled in, it hasn't been.

---

## What this means for the weighing station

**The marinade cannot be weighed on the station's 5 g scale.** Of 16
ingredients in the 2550.5 g batch:

| | |
|---|---|
| unweighable | Yeast extract (10 g), garlic powder (6 g), bhut jholokia (2.5 g), liquid smoke (0 g) |
| marginal, under 4 divisions | black pepper (11 g), chilli flake (13 g), onion powder (14 g) |
| fine | the other 9 |

Bhut jholokia at 2.5 g is **half of one scale division**. And it is worse if
the marinade were weighed per-batch at the station rather than premixed: per
1 kg of meat, ten of the sixteen ingredients fall under two divisions —
garlic powder would be 0.6 g, bhut jholokia 0.25 g.

The marinade has to be premixed in bulk on a fine scale (0.1 g resolution),
and the station should weigh it as a **single** ingredient at 255 g/kg of
meat. That is what the sheet's own structure already implies, since it costs
the marinade as one line.

### A defect this exposed in the app

Checking the CSV against the station turned up a real hole in the code. With a
tolerance of `max(10 g, 2 %)`, an ingredient with a target *below* 10 g has a
tolerance wider than its own target — which means **zero is inside tolerance
and an operator who adds nothing at all passes the step**. Garlic powder at
6 g would have been silently accepted as correct while empty.

This was not hypothetical for the existing recipes either: at the 500 g
minimum base weight, `Salt` at 1.8 % is a 9 g target with a ±10 g window.

Fixed. `Config.unweighable()` now rejects any target under two scale divisions
or with a tolerance at least as wide as the target, and the recipe review
screen refuses to start a batch it cannot enforce — naming the ingredient and
the reason instead of waving it through. Five tests cover it.

---

## Open questions for you

1. Which yield is real — 70/60 %, or 52 %? Everything downstream moves.
2. Is the 1000 g in the product-cost blocks raw meat or finished jerky?
3. Is liquid smoke in the recipe or out?
4. Should the station weigh the premixed marinade as one 255 g/kg ingredient?
   If yes, I can generate the Teriyaki entry for `recipes.json` — it is a
   straightforward conversion, since column 6 is already grams per kilogram of
   meat, so the percentages fall out directly (teriyaki sauce 7.3 %, soy
   4.64 %, honey 4.2 %, and so on).
