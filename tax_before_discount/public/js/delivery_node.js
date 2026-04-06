frappe.ui.form.on("Delivery Note", {
    customer(frm) {
        if (!frm.doc.customer) return;

        frappe.db.get_value(
            "Customer",                  // DocType
            frm.doc.customer,            // document name
            "taxes_and_charges",         // field to fetch
            (r) => {
                if (r && r.taxes_and_charges) {
                    frm.set_value("tax_category", "r.tax_category");
                    frm.set_value("taxes_and_charges", r.taxes_and_charges);
                }
            }
        );
    }
});