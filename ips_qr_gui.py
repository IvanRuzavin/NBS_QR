#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Simple desktop GUI for NBS IPS QR payment PDFs.

Asks for the payer type/payment code, amount, payer name and payment purpose;
the recipient comes from the successful transaction receipt. Creates a reference
from local generation time: YYYYMMDDHHMMSS (IPS QR RO model 00).

Run: python ips_qr_gui.py
Dependencies: requests, reportlab, Pillow and standard-library tkinter.
"""

from __future__ import annotations

import base64
from datetime import datetime
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
import re
import sys
from xml.sax.saxutils import escape
from uuid import uuid4

import requests
from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable, Image, KeepTogether, Paragraph, SimpleDocTemplate,
    Spacer, Table, TableStyle,
)


# ---- Fixed payment information (taken from the attached bank receipt) ----
RECIPIENT = "Sofiia Zhuikova PR Ostalo Obrazovanje Ursa School Beograd"
RECIPIENT_ACCOUNT = "105-0000003369490-26"
# Payment codes used by this application for the selected payer category.
PAYMENT_CODE_INDIVIDUAL = "289"
PAYMENT_CODE_BUSINESS = "221"
VALID_PAYMENT_CODES = frozenset({PAYMENT_CODE_INDIVIDUAL, PAYMENT_CODE_BUSINESS})
CURRENCY = "RSD"
# The receipt has no fixed payment reference. A unique timestamp reference
# is generated for each new payment and used with NBS model 00.

NBS_GENERATE_URL = "https://nbs.rs/QRcode/api/qr/v1/generate/500"

NAVY = colors.HexColor("#13283F")
BLUE = colors.HexColor("#176B83")
LIGHT = colors.HexColor("#F3F7FA")
MUTED = colors.HexColor("#617384")
BORDER = colors.HexColor("#DDE6EB")


def parse_amount(user_value: str) -> Decimal:
    """Accept e.g. 6000, 6.000, 6.000,50, 6000.50, 6,000.50."""
    text = re.sub(r"[ \u00a0\u202f]", "", user_value.strip())
    if not text or not re.fullmatch(r"[\d.,]+", text, flags=re.ASCII):
        raise ValueError("Unesite broj, npr. 6000 ili 6.000,50.")

    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            if not re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d{1,2}", text):
                raise ValueError("Neispravan format iznosa. Primer: 6.000,50.")
            normalized = text.replace(".", "").replace(",", ".")
        else:
            if not re.fullmatch(r"\d{1,3}(?:,\d{3})+\.\d{1,2}", text):
                raise ValueError("Neispravan format iznosa. Primer: 6.000,50.")
            normalized = text.replace(",", "")
    elif "," in text:
        if not re.fullmatch(r"\d+,\d{1,2}", text):
            raise ValueError("Koristite decimalni zarez, npr. 6000,50.")
        normalized = text.replace(",", ".")
    elif "." in text:
        if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", text):
            normalized = text.replace(".", "")
        elif re.fullmatch(r"\d+\.\d{1,2}", text):
            normalized = text
        else:
            raise ValueError("Neispravan format iznosa. Primer: 6.000,50.")
    else:
        normalized = text

    try:
        amount = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError("Neispravan iznos.") from exc

    if not amount.is_finite() or amount <= 0:
        raise ValueError("Iznos mora biti veći od nule.")
    if amount > Decimal("999999999999.99"):
        raise ValueError("Iznos je prevelik.")
    return amount.quantize(Decimal("0.01"))


def require_text(value: str, field_name: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise ValueError(f"Polje '{field_name}' ne sme biti prazno.")
    if "|" in value:
        raise ValueError(f"Polje '{field_name}' ne sme sadržati znak |.")
    limit = 35 if field_name == "Svrha plaćanja" else 70
    if len(value) > limit:
        raise ValueError(f"Polje '{field_name}' je predugačko (maksimalno {limit} znakova).")
    return value


def nbs_amount(amount: Decimal) -> str:
    return f"RSD{amount:.2f}".replace(".", ",")


def display_amount(amount: Decimal) -> str:
    whole, decimals = f"{amount:.2f}".split(".")
    return f"{int(whole):,}".replace(",", ".") + "," + decimals


def make_ips_payload(
    amount: Decimal, payer: str, purpose: str, reference: str,
    payment_code: str = PAYMENT_CODE_INDIVIDUAL,
) -> str:
    if payment_code not in VALID_PAYMENT_CODES:
        raise ValueError("Nepodržana šifra plaćanja.")
    account = RECIPIENT_ACCOUNT.replace("-", "")
    if not re.fullmatch(r"\d{18}", account):
        raise ValueError("Fiksni račun primaoca nije u formatu 18 cifara.")
    if not re.fullmatch(r"\d{14}", reference, flags=re.ASCII):
        raise ValueError("Referenca mora biti datum i vreme sa 14 cifara.")
    return "|".join((
        "K:PR", "V:01", "C:1",
        f"R:{account}",
        f"N:{RECIPIENT}",
        f"I:{nbs_amount(amount)}",
        f"P:{payer}",
        f"SF:{payment_code}",
        f"S:{purpose}",
        f"RO:00{reference}",
    ))


def generate_nbs_qr(payload: str) -> bytes:
    """Ask the NBS service to validate the data and return its QR image."""
    try:
        response = requests.post(
            NBS_GENERATE_URL,
            data=payload.encode("utf-8"),
            headers={
                "Content-Type": "text/plain; charset=UTF-8",
                "Accept": "application/json",
            },
            timeout=(10, 35),
        )
        response.raise_for_status()
        result = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Ne mogu da pristupim NBS servisu: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError("NBS servis nije vratio ispravan JSON odgovor.") from exc

    status = result.get("s", {})
    if str(status.get("code")) != "0":
        details = result.get("e") or []
        if isinstance(details, list):
            description = "; ".join(str(v) for v in details)
        else:
            description = str(details)
        reason = description or status.get("desc", "Nepoznata greška")
        raise RuntimeError(f"NBS nije prihvatio podatke za QR kod: {reason}")

    picture = result.get("i")
    if not picture:
        raise RuntimeError("NBS servis nije vratio sliku QR koda.")
    try:
        qr_png = base64.b64decode(picture, validate=True)
        with PILImage.open(BytesIO(qr_png)) as img:
            img.verify()
        return qr_png
    except Exception as exc:
        raise RuntimeError("NBS servis je vratio neispravnu sliku QR koda.") from exc


def setup_fonts() -> tuple[str, str]:
    """Find an installed font that renders Serbian Latin characters."""
    candidates = []
    # Custom fonts may be placed beside the source script or bundled executable.
    for folder in (Path(__file__).resolve().parent, Path(sys.executable).resolve().parent):
        candidates.append((folder / "DejaVuSans.ttf", folder / "DejaVuSans-Bold.ttf"))

    candidates += [
        (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
         Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
        (Path("C:/Windows/Fonts/arial.ttf"), Path("C:/Windows/Fonts/arialbd.ttf")),
        (Path("C:/Windows/Fonts/segoeui.ttf"), Path("C:/Windows/Fonts/segoeuib.ttf")),
    ]
    mac_fonts = Path("/System/Library/Fonts/Supplemental")
    for base in (mac_fonts, Path("/Library/Fonts"), Path.home() / "Library/Fonts"):
        for a, b in (
            ("Arial.ttf", "Arial Bold.ttf"),
            ("Arial Unicode.ttf", "Arial Bold.ttf"),
            ("Verdana.ttf", "Verdana Bold.ttf"),
            ("Times New Roman.ttf", "Times New Roman Bold.ttf"),
            ("Tahoma.ttf", "Tahoma Bold.ttf"),
        ):
            candidates.append((base / a, base / b))

    for regular, bold in candidates:
        if regular.is_file() and bold.is_file():
            try:
                pdfmetrics.registerFont(TTFont("IPSRegular", str(regular)))
                pdfmetrics.registerFont(TTFont("IPSBold", str(bold)))
                return "IPSRegular", "IPSBold"
            except Exception:
                continue
    raise RuntimeError(
        "Nije pronađen font sa podrškom za srpska slova. "
        "Instalirajte DejaVu Sans ili postavite DejaVuSans.ttf i "
        "DejaVuSans-Bold.ttf pored aplikacije."
    )


def create_pdf(
    output: Path, qr_image_bytes: bytes, amount: Decimal, payer: str, purpose: str,
    reference: str, generated_at: datetime,
    payment_code: str = PAYMENT_CODE_INDIVIDUAL,
) -> None:
    regular, bold = setup_fonts()
    styles = {
        "title": ParagraphStyle("title", fontName=bold, fontSize=22, leading=28,
                                textColor=NAVY, spaceAfter=3),
        "subtitle": ParagraphStyle("subtitle", fontName=regular, fontSize=10,
                                   leading=15, textColor=MUTED),
        "label": ParagraphStyle("label", fontName=regular, fontSize=9,
                                leading=13, textColor=MUTED),
        "name": ParagraphStyle("name", fontName=bold, fontSize=11,
                               leading=16, textColor=NAVY),
        "value": ParagraphStyle("value", fontName=regular, fontSize=10.3,
                                leading=15, textColor=NAVY),
        "amount": ParagraphStyle("amount", fontName=bold, fontSize=23,
                                 leading=30, textColor=BLUE),
        "qr_tag": ParagraphStyle("qr_tag", fontName=bold, fontSize=11,
                                 leading=15, alignment=TA_CENTER, textColor=NAVY),
        "small": ParagraphStyle("small", fontName=regular, fontSize=8.5,
                                leading=13, textColor=MUTED),
    }

    def para(text: str, style: str):
        return Paragraph(escape(text), styles[style])

    doc = SimpleDocTemplate(
        str(output), pagesize=A4,
        rightMargin=22 * mm, leftMargin=22 * mm,
        topMargin=20 * mm, bottomMargin=18 * mm,
        title="IPS QR - nalog za placanje",
        author="IPS QR generator",
    )
    usable_width = A4[0] - 44 * mm
    story = [
        para("IPS QR PLAĆANJE", "title"),
        para("Pripremljeni nalog za uplatu u dinarima", "subtitle"),
        Spacer(1, 6 * mm),
        HRFlowable(width="100%", thickness=1.25, color=BORDER),
        Spacer(1, 11 * mm),
    ]

    qr = Image(BytesIO(qr_image_bytes), width=66 * mm, height=66 * mm)
    qr.hAlign = "CENTER"
    qr_column = [qr, Spacer(1, 2 * mm), para("NBS IPS QR", "qr_tag")]
    data_column = [
        para("IZNOS ZA UPLATU", "label"),
        Spacer(1, 2 * mm),
        para(f"{display_amount(amount)} RSD", "amount"),
        Spacer(1, 11 * mm),
        para("PRIMALAC", "label"),
        Spacer(1, 2 * mm),
        para(RECIPIENT, "name"),
        Spacer(1, 7 * mm),
        para("RAČUN PRIMAOCA", "label"),
        Spacer(1, 2 * mm),
        para(RECIPIENT_ACCOUNT, "name"),
    ]
    main = Table([[qr_column, data_column]],
                 colWidths=[80 * mm, usable_width - 80 * mm],
                 hAlign="LEFT")
    main.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("RIGHTPADDING", (0, 0), (0, 0), 12),
        ("LEFTPADDING", (1, 0), (1, 0), 8),
        ("RIGHTPADDING", (1, 0), (1, 0), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.extend([main, Spacer(1, 13 * mm)])

    field_rows = [
        ("Naziv platioca", payer),
        ("Svrha plaćanja", purpose),
        ("Šifra plaćanja", payment_code),
        ("Valuta", CURRENCY),
        ("Model i poziv na broj", f"00 / {reference}"),
        ("Generisano", generated_at.strftime("%d.%m.%Y. %H:%M:%S")),
    ]
    cells = [[para(k, "label"), para(v, "value")] for k, v in field_rows]
    details = Table(cells, colWidths=[46 * mm, usable_width - 46 * mm], hAlign="LEFT")
    rule_list = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 11),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 11),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, BORDER),
    ]
    for i in range(len(field_rows)):
        if i % 2 == 0:
            rule_list.append(("BACKGROUND", (0, i), (-1, i), LIGHT))
    details.setStyle(TableStyle(rule_list))
    story.extend([
        details,
        Spacer(1, 10 * mm),
        para("Skenirajte NBS IPS QR kod u aplikaciji banke i proverite podatke pre potvrde.", "small"),
        Spacer(1, 2 * mm),
        para("Ovaj dokument nije potvrda o izvršenom plaćanju.", "small"),
    ])
    doc.build(story)


# -------- Desktop user interface --------
BG = "#F2F5F9"
PANEL = "#FFFFFF"
INK = "#142A43"
SUBTLE = "#637487"
ACCENT = "#116B82"
ACCENT_HOVER = "#0D576B"
STROKE = "#DCE4EC"


class PaymentGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("IPS QR - Nalog za uplatu")
        self.geometry("560x755")
        self.minsize(530, 720)
        self.configure(bg=BG)
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._is_busy = False
        self._build_interface()

    def _build_interface(self) -> None:
        root = tk.Frame(self, bg=BG, padx=28, pady=16)
        root.pack(fill="both", expand=True)

        tk.Label(root, text="IPS QR PLAĆANJE", bg=BG, fg=INK,
                 font=("Arial", 22, "bold"), anchor="w").pack(fill="x")
        tk.Label(root, text="Kreiranje QR koda i PDF naloga za uplatu",
                 bg=BG, fg=SUBTLE, font=("Arial", 10),
                 anchor="w").pack(fill="x", pady=(4, 12))

        card = tk.Frame(root, bg=PANEL, highlightthickness=1, highlightbackground=STROKE)
        card.pack(fill="x")
        content = tk.Frame(card, bg=PANEL, padx=21, pady=13)
        content.pack(fill="x")

        tk.Label(content, text="PRIMALAC (FIKSNI PODACI)", bg=PANEL, fg=SUBTLE,
                 font=("Arial", 9, "bold"), anchor="w").pack(fill="x")
        tk.Label(content, text=RECIPIENT, bg=PANEL, fg=INK,
                 font=("Arial", 10, "bold"), wraplength=454,
                 justify="left", anchor="w").pack(fill="x", pady=(7, 3))
        self.recipient_info = tk.Label(
            content, text="", bg=PANEL, fg=SUBTLE,
            font=("Arial", 9), anchor="w",
        )
        self.recipient_info.pack(fill="x")

        ttk.Separator(content, orient="horizontal").pack(fill="x", pady=(12, 9))

        tk.Label(content, text="TIP PLATIOCA", bg=PANEL, fg=SUBTLE,
                 font=("Arial", 9, "bold"), anchor="w").pack(fill="x", pady=(0, 5))
        self.payer_type = tk.StringVar(value="individual")
        self.payer_type_row = tk.Frame(content, bg=PANEL)
        self.payer_type_row.pack(fill="x", pady=(0, 7))
        for label, value in (
            ("Fizičko lice", "individual"),
            ("Pravno lice / preduzetnik", "business"),
        ):
            tk.Radiobutton(
                self.payer_type_row, text=label, value=value,
                variable=self.payer_type, command=self._update_payment_code,
                bg=PANEL, fg=INK, selectcolor=PANEL, activebackground=PANEL,
                activeforeground=INK, font=("Arial", 10), cursor="hand2",
                highlightthickness=0, bd=0,
            ).pack(side="left", padx=(0, 18))

        self.amount_entry = self._input(content, "Iznos (RSD)", "Primer: 6.000,00")
        self.payer_entry = self._input(content, "Naziv platioca", "Ime i prezime ili naziv firme")
        self.purpose_entry = self._input(content, "Svrha plaćanja", "Maksimalno 35 znakova")
        self.amount_entry.focus_set()
        self._update_payment_code()

        tk.Label(content, text="POZIV NA BROJ", bg=PANEL, fg=SUBTLE,
                 font=("Arial", 9, "bold"), anchor="w").pack(fill="x", pady=(4, 4))
        tk.Label(content, text="Automatski: datum i vreme generisanja (model 00)",
                 bg=PANEL, fg=INK, font=("Arial", 9),
                 anchor="w").pack(fill="x")

        self.generate_btn = tk.Button(
            root, text="Generiši PDF sa QR kodom", command=self.generate,
            bg=ACCENT, fg="white", activebackground=ACCENT_HOVER,
            activeforeground="white", relief="flat", cursor="hand2",
            font=("Arial", 11, "bold"), padx=12, pady=14,
            borderwidth=0, highlightthickness=0,
        )
        self.generate_btn.pack(fill="x", pady=(14, 5))

        self.progress = ttk.Progressbar(root, mode="indeterminate", length=220)
        self.progress.pack(fill="x", pady=(0, 4))
        self.progress.pack_forget()

        self.status = tk.StringVar(value="Spremno za generisanje PDF-a.")
        tk.Label(root, textvariable=self.status, bg=BG, fg=SUBTLE,
                 font=("Arial", 9), anchor="w", wraplength=490,
                 justify="left").pack(fill="x", pady=(5, 0))

        self.footer_label = tk.Label(
            root, text="QR kod popunjava nalog u aplikaciji banke; ne izvršava plaćanje.",
            bg=BG, fg=SUBTLE, font=("Arial", 8), anchor="w",
            justify="left", wraplength=490,
        )
        self.footer_label.pack(side="bottom", fill="x")

        for entry in (self.amount_entry, self.payer_entry, self.purpose_entry):
            entry.bind("<Return>", lambda _event: self.generate())

    @staticmethod
    def _input(parent: tk.Frame, label: str, hint: str) -> tk.Entry:
        tk.Label(parent, text=label, bg=PANEL, fg=INK,
                 font=("Arial", 10, "bold"), anchor="w").pack(fill="x", pady=(0, 5))
        field = tk.Entry(parent, font=("Arial", 12), fg=INK, bg="white",
                         insertbackground=INK, relief="solid", bd=1,
                         highlightthickness=1, highlightbackground=STROKE,
                         highlightcolor=ACCENT)
        field.pack(fill="x", ipady=5)
        tk.Label(parent, text=hint, bg=PANEL, fg=SUBTLE,
                 font=("Arial", 8), anchor="w").pack(fill="x", pady=(3, 7))
        return field

    def _selected_payment_code(self) -> str:
        if self.payer_type.get() == "individual":
            return PAYMENT_CODE_INDIVIDUAL
        if self.payer_type.get() == "business":
            return PAYMENT_CODE_BUSINESS
        raise ValueError("Izaberite važeću vrstu platioca.")

    def _update_payment_code(self) -> None:
        self.recipient_info.configure(
            text=f"Račun: {RECIPIENT_ACCOUNT}  •  Šifra: {self._selected_payment_code()}"
        )

    def generate(self) -> None:
        if self._is_busy:
            return
        try:
            amount = parse_amount(self.amount_entry.get())
            payer = require_text(self.payer_entry.get(), "Naziv platioca")
            purpose = require_text(self.purpose_entry.get(), "Svrha plaćanja")
            generated_at = datetime.now().astimezone()
            reference = generated_at.strftime("%Y%m%d%H%M%S")
            payment_code = self._selected_payment_code()
            payload = make_ips_payload(amount, payer, purpose, reference, payment_code)
        except ValueError as exc:
            messagebox.showerror("Proverite podatke", str(exc), parent=self)
            return

        proposed_name = f"IPS_QR_{reference}.pdf"
        selected = filedialog.asksaveasfilename(
            parent=self, title="Sačuvaj IPS QR PDF", initialdir=str(Path.home()),
            initialfile=proposed_name, defaultextension=".pdf",
            filetypes=[("PDF dokument", "*.pdf")],
        )
        if not selected:
            return

        self._is_busy = True
        self.generate_btn.configure(state="disabled", text="Generisanje u toku...")
        self.status.set("Povezivanje sa NBS servisom i kreiranje PDF-a...")
        self.progress.pack(fill="x", pady=(0, 4), after=self.generate_btn)
        self.progress.start(12)

        thread = threading.Thread(
            target=self._worker,
            args=(Path(selected), payload, amount, payer, purpose, reference,
                  generated_at, payment_code),
            daemon=True,
        )
        thread.start()
        self.after(100, self._check_events)

    def _worker(self, output: Path, payload: str, amount: Decimal, payer: str,
                purpose: str, reference: str, generated_at: datetime,
                payment_code: str) -> None:
        temp = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
        try:
            # Check font availability before making any network request.
            setup_fonts()
            qr_png = generate_nbs_qr(payload)
            create_pdf(temp, qr_png, amount, payer, purpose, reference,
                       generated_at, payment_code)
            os.replace(temp, output)
            self._events.put(("ok", (output, reference)))
        except Exception as exc:
            self._events.put(("error", str(exc)))
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def _check_events(self) -> None:
        try:
            kind, detail = self._events.get_nowait()
        except queue.Empty:
            if self._is_busy:
                self.after(100, self._check_events)
            return
        self._is_busy = False
        self.progress.stop()
        self.progress.pack_forget()
        self.generate_btn.configure(state="normal", text="Generiši PDF sa QR kodom")
        if kind == "ok":
            path, reference = detail
            self.status.set(f"PDF sačuvan: {path.name}  |  Poziv na broj: {reference}")
            messagebox.showinfo("PDF je spreman",
                                f"PDF je uspešno sačuvan:\n{path}\n\n"
                                f"Poziv na broj (model 00): {reference}", parent=self)
        else:
            self.status.set("Generisanje nije uspelo. Proverite podatke ili internet.")
            messagebox.showerror("Greška pri generisanju", str(detail), parent=self)


def main() -> None:
    app = PaymentGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
