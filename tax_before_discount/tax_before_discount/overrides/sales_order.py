import frappe
import json
from frappe import _
from frappe.utils import flt
from erpnext.controllers.accounts_controller import get_taxes_and_charges
from erpnext.controllers.taxes_and_totals import calculate_taxes_and_totals


# ─────────────────────────────────────────────
#  HOOKS
# ─────────────────────────────────────────────

def custom_on_update(doc, method):
    """
    Hook: Sales Invoice / Sales Order — on_update
    Order matters:
      1. Basic amounts first (pure field population, no totals impact)
      2. Sales team from customer
      3. Tax template from customer  ← runs calculate_taxes_and_totals internally
      4. Pre-discount tax override   ← must run AFTER step 3 to avoid being wiped
    """
    settings = frappe.get_single("Tax Before Discount Settings")

    if settings.enable_basic_amount:
        _calculate_basic_amounts(doc)

    if not is_enabled_tax_before_discount(doc):
        return

    _set_order_booker(doc)
    _set_tax_template(doc)              # sets template + runs ERPNext's calculate_taxes_and_totals
    calculate_tax_before_discount(doc, method)   # override with pre-discount tax amounts


def calculate_tax_before_discount(doc, method):
    """
    Hook: Sales Order — validate
    Can also be called directly from custom_on_update (see above).
    1. Fetches discount_account from applied Pricing Rule per item.
    2. Recalculates taxes based on pre-discount item totals.
    """
    # Guard first — no point doing anything else if feature is off
    if not is_enabled_tax_before_discount(doc):
        return

    _set_discount_account_from_pricing_rule(doc)

    settings = frappe.get_single("Tax Before Discount Settings")

    if not settings.apply_to_all_companies:
        if doc.company != settings.company:
            return

    if not _has_discount(doc):
        return

    pre_discount_total = _get_pre_discount_net_total(doc)
    if not pre_discount_total:
        return

    post_discount_total = flt(doc.net_total)
    if not post_discount_total:
        return

    _recalculate_taxes(doc, pre_discount_total)
    _recalculate_totals(doc)

    # Update sales team allocated_amount now that grand_total is finalised
    _update_sales_team_amounts(doc)

    frappe.msgprint(
        _("Taxes calculated on pre-discount amount: {0}").format(
            frappe.format_value(pre_discount_total, {"fieldtype": "Currency"})
        ),
        indicator="green",
        alert=True
    )


# ─────────────────────────────────────────────
#  PRICING RULE / DISCOUNT ACCOUNT
# ─────────────────────────────────────────────

def _set_discount_account_from_pricing_rule(doc):
    """
    For each Sales Order / Invoice item row:
      1. Try item.pricing_rules (JSON array or comma string)
      2. Try item.pricing_rule  (single value field)
      3. Fall back to DB lookup by item_code + company
    Sets item.discount_account if found and not already set.
    """
    for item in doc.items:
        if item.get("discount_account"):
            continue

        discount_account = None

        # Strategy 1: pricing_rules field (v15 JSON array)
        rule_names = _parse_pricing_rules_field(item.get("pricing_rules"))
        if rule_names:
            discount_account = _fetch_discount_account_from_rules(rule_names)

        # Strategy 2: pricing_rule single field
        if not discount_account:
            single_rule = item.get("pricing_rule")
            if single_rule:
                discount_account = _fetch_discount_account_from_rules([single_rule])

        # Strategy 3: lookup by item_code in Pricing Rule Item Code child table
        if not discount_account:
            discount_account = _fetch_discount_account_by_item(item.item_code, doc.company)

        if discount_account:
            item.discount_account = discount_account

        # DEBUG — remove after confirming it works
        frappe.log_error(
            title="SO Item Pricing Rule Debug",
            message=frappe.as_json({
                "item_code":              item.item_code,
                "pricing_rules_raw":      item.get("pricing_rules"),
                "pricing_rule_raw":       item.get("pricing_rule"),
                "parsed_rules":           _parse_pricing_rules_field(item.get("pricing_rules")),
                "discount_account_found": discount_account,
                "discount_account_on_item": item.get("discount_account"),
            })
        )


def _parse_pricing_rules_field(value):
    """
    Parses the pricing_rules field which ERPNext v15 stores as:
      - None / empty string  → []
      - JSON array string    → '["PRULE-0001"]'  → ["PRULE-0001"]
      - Comma-separated      → "PRULE-0001,PRULE-0002" → [...]
      - Plain single string  → "PRULE-0001" → ["PRULE-0001"]
    """
    if not value:
        return []

    value = value.strip()

    if value.startswith("["):
        try:
            parsed = json.loads(value)
            return [r for r in parsed if r]
        except Exception:
            pass

    return [r.strip() for r in value.split(",") if r.strip()]


def _fetch_discount_account_from_rules(rule_names):
    """
    Returns the first discount_account found across the given Pricing Rule names.
    """
    for rule_name in rule_names:
        if not rule_name:
            continue
        result = frappe.db.get_value("Pricing Rule", rule_name, "discount_account")
        if result:
            return result
    return None


def _fetch_discount_account_by_item(item_code, company):
    """
    Strategy 3 fallback.
    Searches Pricing Rule Item Code child table for rules that apply to this
    item_code, then fetches discount_account from the parent Pricing Rule
    filtered by company, selling=1, not disabled, and discount_account is set.
    """
    if not item_code:
        return None

    rules_with_item = frappe.db.get_all(
        "Pricing Rule Item Code",
        filters={"item_code": item_code},
        pluck="parent"
    )

    if not rules_with_item:
        return None

    result = frappe.db.get_value(
        "Pricing Rule",
        {
            "name":             ["in", rules_with_item],
            "company":          company,
            "selling":          1,
            "disable":          0,
            "discount_account": ["not in", ["", None]],
        },
        "discount_account"
    )
    return result or None


# ─────────────────────────────────────────────
#  DISCOUNT / PRE-DISCOUNT HELPERS
# ─────────────────────────────────────────────

def _has_discount(doc):
    """Returns True if any discount exists at order or item level."""
    if flt(doc.discount_amount) or flt(doc.additional_discount_percentage):
        return True

    for item in doc.items:
        if flt(item.discount_percentage) or flt(item.discount_amount):
            return True

    return False


def _get_pre_discount_net_total(doc):
    """
    Computes net total BEFORE any discount.
    Uses price_list_rate * qty per item.
    Falls back to rate * qty if price_list_rate is not set.
    """
    total = 0.0
    for item in doc.items:
        base_rate = flt(item.price_list_rate) if flt(item.price_list_rate) else flt(item.rate)
        total += base_rate * flt(item.qty)
    return total


# ─────────────────────────────────────────────
#  TAX RECALCULATION
# ─────────────────────────────────────────────

def _recalculate_taxes(doc, pre_discount_total):
    """
    Recalculates tax rows using pre_discount_total as base.

    charge_type handling:
      - On Net Total          → recalculate using pre_discount_total
      - Actual                → fixed amount, only update running total fields
      - On Previous Row Total → recalculate based on previous row's running total
      - On Previous Row Amount→ recalculate based on previous row's tax_amount
      - anything else         → log and skip
    """
    running_total = flt(pre_discount_total)
    prev_row_total  = 0.0
    prev_row_amount = 0.0

    for tax in doc.taxes:

        if tax.charge_type == "On Net Total":
            tax_rate       = flt(tax.rate)
            new_tax_amount = flt(
                (tax_rate / 100) * pre_discount_total,
                tax.precision("tax_amount")
            )

        elif tax.charge_type == "On Previous Row Total":
            tax_rate       = flt(tax.rate)
            new_tax_amount = flt(
                (tax_rate / 100) * prev_row_total,
                tax.precision("tax_amount")
            )

        elif tax.charge_type == "On Previous Row Amount":
            tax_rate       = flt(tax.rate)
            new_tax_amount = flt(
                (tax_rate / 100) * prev_row_amount,
                tax.precision("tax_amount")
            )

        elif tax.charge_type == "Actual":
            new_tax_amount = flt(tax.tax_amount)

        else:
            frappe.log_error(
                title="Tax Before Discount: Unsupported charge_type",
                message=(
                    f"charge_type '{tax.charge_type}' on tax row {tax.idx} "
                    f"({tax.description}) is not handled — row skipped."
                )
            )
            prev_row_total  = running_total
            prev_row_amount = 0.0
            continue

        tax.tax_amount                            = new_tax_amount
        tax.base_tax_amount                       = new_tax_amount
        tax.tax_amount_after_discount_amount      = new_tax_amount
        tax.base_tax_amount_after_discount_amount = new_tax_amount

        running_total = flt(running_total + new_tax_amount, tax.precision("total"))
        tax.total      = running_total
        tax.base_total = running_total

        prev_row_total  = running_total
        prev_row_amount = new_tax_amount


def _recalculate_totals(doc):
    """
    Recomputes order-level totals after tax rows are adjusted.
    net_total (post-discount) is intentionally left intact.
    """
    total_taxes = sum(flt(t.tax_amount) for t in doc.taxes)

    doc.total_taxes_and_charges      = flt(total_taxes, doc.precision("total_taxes_and_charges"))
    doc.base_total_taxes_and_charges = doc.total_taxes_and_charges

    grand_total = flt(flt(doc.net_total) + total_taxes, doc.precision("grand_total"))
    doc.grand_total      = grand_total
    doc.base_grand_total = flt(
        grand_total * flt(doc.conversion_rate or 1),
        doc.precision("base_grand_total")
    )

    rounded     = flt(round(grand_total), doc.precision("rounded_total"))
    rounding_adj = flt(rounded - grand_total, doc.precision("rounding_adjustment"))

    doc.rounded_total            = rounded
    doc.base_rounded_total       = rounded
    doc.rounding_adjustment      = rounding_adj
    doc.base_rounding_adjustment = rounding_adj


# ─────────────────────────────────────────────
#  SALES TEAM
# ─────────────────────────────────────────────

def _set_order_booker(doc, method=None):
    """
    Populates the Sales Team child table from the Customer's Sales Team.
    allocated_amount is intentionally set to 0 here because grand_total
    is not yet finalised at this point — _update_sales_team_amounts()
    corrects it after taxes are recalculated.
    """
    if not doc.customer:
        return

    sales_team_rows = frappe.db.get_all(
        "Sales Team",
        filters={"parent": doc.customer, "parenttype": "Customer"},
        fields=["sales_person", "allocated_percentage"],
        order_by="idx asc"
    )

    if not sales_team_rows:
        return

    doc.set("sales_team", [])

    for row in sales_team_rows:
        if not row.sales_person:
            continue

        doc.append("sales_team", {
            "sales_person":         row.sales_person,
            "allocated_percentage": row.allocated_percentage or 0,
            "allocated_amount":     0,   # corrected in _update_sales_team_amounts
        })


def _update_sales_team_amounts(doc):
    """
    Called after grand_total is finalised to set correct allocated_amount
    on every Sales Team row.
    """
    for row in doc.sales_team:
        row.allocated_amount = flt(
            (doc.grand_total or 0) * flt(row.allocated_percentage or 0) / 100
        )


# ─────────────────────────────────────────────
#  TAX TEMPLATE
# ─────────────────────────────────────────────

def _set_tax_template(doc):
    """
    Applies the Customer's default tax template to the document.
    Internally calls ERPNext's calculate_taxes_and_totals — this will be
    overridden by calculate_tax_before_discount() which runs after this.
    """
    if not doc.customer:
        return

    tax_template = frappe.db.get_value("Customer", doc.customer, "taxes_and_charges")

    if tax_template:
        doc.taxes_and_charges = tax_template
        doc.set("taxes", get_taxes_and_charges("Sales Taxes and Charges Template", tax_template))
        calculate_taxes_and_totals(doc)
    else:
        doc.taxes_and_charges = ""
        doc.set("taxes", [])


# ─────────────────────────────────────────────
#  BASIC AMOUNTS
# ─────────────────────────────────────────────

def _calculate_basic_amounts(doc):
    """
    Triggered on validate of Sales Invoice.

    For each row in items:
        basic_amount   = price_list_rate * qty        (pre-discount gross)
        discount_amount = basic_amount - amount        (actual row-level discount)

    Then sets totals on the parent document.

    NOTE: row.discount_amount in ERPNext is per-unit; the true row discount
    is (basic_amount - row.amount), so we use that to avoid multiplying again.
    """
    total_basic_amount    = 0.0
    total_discount_amount = 0.0

    for row in doc.items:
        price_list_rate = flt(row.price_list_rate)
        qty             = flt(row.qty)

        row.basic_amount        = price_list_rate * qty
        row_discount            = flt(row.basic_amount) - flt(row.amount)
        total_basic_amount    += row.basic_amount
        total_discount_amount += row_discount

    doc.total_basic_amount    = flt(total_basic_amount)
    doc.total_discount_amount = flt(total_discount_amount)


# ─────────────────────────────────────────────
#  FEATURE FLAG
# ─────────────────────────────────────────────

def is_enabled_tax_before_discount(doc):
    """
    Returns True if the Customer linked to this document has
    enable_tax_before_discount checked.
    Returns False safely if customer is not set.
    """
    if not doc.customer:
        return False

    return bool(
        frappe.db.get_value("Customer", doc.customer, "enable_tax_before_discount")
    )