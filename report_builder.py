import json
from pathlib import Path
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

DATA_ROOT = Path("data")
TXT_DIR = DATA_ROOT / "results"
PDF_DIR = DATA_ROOT / "pdf"
TXT_DIR.mkdir(parents=True, exist_ok=True)
PDF_DIR.mkdir(parents=True, exist_ok=True)

def safe_name(value: str) -> str:
    return "".join(c for c in value if c.isalnum() or c in ("_", "-", ".")) or "target"

def build_txt(target: str, dossier: dict) -> str:
    path = TXT_DIR / f"{safe_name(target)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    path.write_text(json.dumps(dossier, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)

def build_pdf(target: str, dossier: dict) -> str:
    path = PDF_DIR / f"{safe_name(target)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    styles = getSampleStyleSheet()
    title = ParagraphStyle("title", parent=styles["Title"], alignment=TA_CENTER, fontSize=18, leading=22)
    h = ParagraphStyle("h", parent=styles["Heading2"], fontSize=12, leading=16)
    n = ParagraphStyle("n", parent=styles["Normal"], fontSize=9, leading=12)

    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=28, rightMargin=28, topMargin=28, bottomMargin=28)
    story = [
        Paragraph("NEXARA DOSSIER", title),
        Spacer(1, 8),
        Paragraph(f"<b>Target:</b> {target}", n),
        Paragraph(f"<b>Generated:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", n),
        Spacer(1, 10),
        Paragraph("Summary", h),
        Paragraph(dossier["summary"]["short"], n),
        Spacer(1, 8),
        Paragraph("Providers", h),
    ]
    for item in dossier.get("providers", []):
        story.append(Paragraph(json.dumps(item, ensure_ascii=False)[:3000], n))
        story.append(Spacer(1, 5))
    doc.build(story)
    return str(path)
