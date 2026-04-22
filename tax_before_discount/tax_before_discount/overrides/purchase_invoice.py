import frappe
from frappe import _
from frappe.utils import flt


def calculate_tax_before_discount(doc, method):
    settings = frappe.get_single("Tax Before Discount Settings")

    if not settings.enabled:
        return

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

    frappe.msgprint(
        _("Taxes calculated on pre-discount amount: {0}").format(
            frappe.format_value(pre_discount_total, {"fieldtype": "Currency"})
        ),
        indicator="green",
        alert=True
    )


def _has_discount(doc):
    if flt(doc.discount_amount) or flt(doc.additional_discount_percentage):
        return True

    for item in doc.items:
        if flt(item.discount_percentage) or flt(item.discount_amount):
            return True

    return False


def _get_pre_discount_net_total(doc):
    total = 0.0
    for item in doc.items:
        base_rate = flt(item.price_list_rate) if flt(item.price_list_rate) else flt(item.rate)
        total += base_rate * flt(item.qty)
    return total


def _recalculate_taxes(doc, pre_discount_total):
    running_total = flt(pre_discount_total)

    for tax in doc.taxes:

        if tax.charge_type == "On Net Total":
            tax_rate = flt(tax.rate)
            new_tax_amount = flt(
                (tax_rate / 100) * pre_discount_total,
                tax.precision("tax_amount")
            )

            tax.tax_amount                            = new_tax_amount
            tax.base_tax_amount                       = new_tax_amount
            tax.tax_amount_after_discount_amount      = new_tax_amount
            tax.base_tax_amount_after_discount_amount = new_tax_amount

            running_total = flt(running_total + new_tax_amount, tax.precision("total"))
            tax.total      = running_total
            tax.base_total = running_total

        elif tax.charge_type == "Actual":
            running_total = flt(running_total + flt(tax.tax_amount), tax.precision("total"))
            tax.total      = running_total
            tax.base_total = running_total


def _recalculate_totals(doc):
    total_taxes = 0.0
    taxes_added = 0.0
    taxes_deducted = 0.0

    for tax in doc.taxes:
        tax_amount = flt(tax.tax_amount)
        total_taxes += tax_amount

        if tax_amount >= 0:
            taxes_added += tax_amount
        else:
            taxes_deducted += abs(tax_amount)

    doc.total_taxes_and_charges      = flt(total_taxes, doc.precision("total_taxes_and_charges"))
    doc.base_total_taxes_and_charges = doc.total_taxes_and_charges

    if hasattr(doc, "taxes_and_charges_added"):
        doc.taxes_and_charges_added         = flt(taxes_added, doc.precision("taxes_and_charges_added"))
        doc.base_taxes_and_charges_added    = doc.taxes_and_charges_added

    if hasattr(doc, "taxes_and_charges_deducted"):
        doc.taxes_and_charges_deducted      = flt(taxes_deducted, doc.precision("taxes_and_charges_deducted"))
        doc.base_taxes_and_charges_deducted = doc.taxes_and_charges_deducted

    grand_total = flt(flt(doc.net_total) + total_taxes, doc.precision("grand_total"))
    doc.grand_total      = grand_total
    doc.base_grand_total = flt(
        grand_total * flt(doc.conversion_rate or 1),
        doc.precision("base_grand_total")
    )

    rounded = flt(round(grand_total), doc.precision("rounded_total"))
    rounding_adj = flt(rounded - grand_total, doc.precision("rounding_adjustment"))

    doc.rounded_total            = rounded
    doc.base_rounded_total       = rounded
    doc.rounding_adjustment      = rounding_adj
    doc.base_rounding_adjustment = rounding_adj

    if not doc.disable_rounded_total:
        doc.outstanding_amount = rounded
    else:
        doc.outstanding_amount = grand_total
