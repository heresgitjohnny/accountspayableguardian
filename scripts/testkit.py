"""Build realistic test invoices (PDF) and vendor emails (.eml) without extra libraries."""
import uuid
from email.message import EmailMessage
from email.utils import formatdate, make_msgid


def _pdf_escape(s):
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(lines, title="TAX INVOICE"):
    """Single-page text PDF (Helvetica). Textract reads these like a scanned invoice."""
    y, ops = 790, ["BT", "/F1 20 Tf", f"50 {800} Td", f"({_pdf_escape(title)}) Tj", "ET"]
    y = 760
    for line in lines:
        ops += ["BT", "/F1 11 Tf", f"50 {y} Td", f"({_pdf_escape(line)}) Tj", "ET"]
        y -= 18
    stream = "\n".join(ops).encode("latin-1", "replace")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def invoice_lines(vendor_name, vendor_abn, inv_no, po, amount, notes=None, bank=None, items=None):
    lines = [
        f"From: {vendor_name}   ABN {vendor_abn}",
        "Bill To: Example Pty Ltd, Accounts Payable",
        f"Invoice Number: {inv_no}",
        "Invoice Date: 24/11/2026",
        "Due Date: 24/12/2026",
    ]
    if po:
        lines.append(f"PO Number: {po}")
    lines += ["", "Description                         Qty     Amount"]
    for desc, qty, amt in (items or [("Goods as per purchase order", 1, amount)]):
        lines.append(f"{desc:<34}  {qty:<6}  ${amt:,.2f}")
    lines += ["", f"Subtotal: ${amount / 1.1:,.2f}", f"GST: ${amount - amount / 1.1:,.2f}", f"Total: ${amount:,.2f}",
              f"Amount Due: ${amount:,.2f}"]
    if bank:
        lines += ["", f"Payment details: {bank}"]
    if notes:
        lines += ["", "Notes:"] + [notes[i:i + 90] for i in range(0, len(notes), 90)]
    return lines


def make_email(from_addr, to_addr, subject, body, pdf_bytes, auth="pass", forged_auth_header=False,
               filename="invoice.pdf"):
    msg = EmailMessage()
    domain = from_addr.split("@")[-1]
    if auth == "pass":
        top = (f"amazonses.com; spf=pass (spfCheck: domain of {domain} designates sender) smtp.mailfrom={from_addr}; "
               f"dkim=pass header.i=@{domain}; dmarc=pass header.from={domain}")
    else:
        top = (f"amazonses.com; spf=fail (spfCheck: domain of {domain} does not designate sender) "
               f"smtp.mailfrom={from_addr}; dkim=none; dmarc=fail header.from={domain}")
    msg["Authentication-Results"] = top
    if forged_auth_header:  # attacker-supplied header lower in the message
        msg["Authentication-Results"] = f"amazonses.com; spf=pass smtp.mailfrom={from_addr}; dkim=pass; dmarc=pass"
    msg["X-SES-Spam-Verdict"] = "PASS"
    msg["X-SES-Virus-Verdict"] = "PASS"
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=domain)
    msg.set_content(body)
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=filename)
    return msg.as_bytes()


def run_id():
    return uuid.uuid4().hex[:6].upper()
