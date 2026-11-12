#!/usr/bin/env python3
"""
Lab Equipment Storage layout.

  Page 1  A3 landscape picture layout: one column per cabinet, one band per shelf,
          one cell per slot - OEM photo (background removed), bin location,
          register number and description.
  Page 2  A3 portrait register table: Location / Register No / Description.

  -> layouts/lab_equipment_storage_a3.pdf

Everything is driven by layouts/lab-equipment-storage/items.csv, so a
rearrangement or new item is a CSV edit and a re-run:

  bin_location  B.S.P (bin, shelf, position). Rows sharing a location share
                that slot. A row with only a location is an empty slot.
  image         file stem in images/raw (any of jpg/png/webp). The cut-out is
                made once into images/cutout/<stem>.png and reused; delete the
                cut-out to redo it.

python lab_equipment_storage.py            # cut out new photos, build the PDF
"""

import csv
import os
from collections import OrderedDict

from PIL import Image
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A3, A4, landscape
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

TECHNOLOGY_BLUE = HexColor("#2878F5")
DEEP_SEA_BLUE = HexColor("#051233")
STEEL_GREY = HexColor("#828C96")
PANEL_BG = HexColor("#F2F7FF")
RULE = HexColor("#C9D6EA")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DATA_DIR = os.path.join(REPO, "data", "lab-equipment-storage")
CSV_PATH = os.path.join(DATA_DIR, "items.csv")          # written by build.py from layout.json
RAW_DIR = os.path.join(REPO, "data", "raw")             # new raw photos only (PC); usually empty
CUT_DIR = os.path.join(REPO, "data", "images")
OUT_DIR = os.path.join(REPO, "downloads")
OUT_PDF = os.path.join(OUT_DIR, "lab_equipment_storage_a3.pdf")
LOGO = os.path.join(REPO, "data", "logo_full.png")

TITLE = "LAB EQUIPMENT STORAGE"
SUBTITLE = "PLEASE RETURN TO CORRECT LOCATION"


# ------------------------------------------------------------------ data
def load_items():
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        return [{k: (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]


def build_tree(items):
    """{bin: {shelf: {pos: [items]}}} in numeric order; empty slots kept."""
    tree = {}
    for it in items:
        b, s, p = (int(x) for x in it["bin_location"].split("."))
        tree.setdefault(b, {}).setdefault(s, {}).setdefault(p, [])
        if it["description"]:
            tree[b][s][p].append(it)
    ordered = OrderedDict()
    for b in sorted(tree):
        ordered[b] = OrderedDict()
        for s in sorted(tree[b]):
            ordered[b][s] = OrderedDict((p, tree[b][s][p]) for p in sorted(tree[b][s]))
    return ordered


# ------------------------------------------------------------- cut-outs
def raw_path(stem):
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        p = os.path.join(RAW_DIR, stem + ext)
        if os.path.exists(p):
            return p
    return None


# photos on a light grey studio backdrop that the white flood cannot clear
MODEL_ONLY = {"1.2.3_AM00357"}


def make_cutouts(items):
    """Background-remove every raw photo that has no cut-out yet."""
    os.makedirs(CUT_DIR, exist_ok=True)
    if not os.path.isdir(RAW_DIR):
        return
    todo = []
    for it in items:
        stem = it["image"]
        if not stem:
            continue
        src = raw_path(stem)
        dst = os.path.join(CUT_DIR, stem + ".png")
        if src and (not os.path.exists(dst) or os.path.getmtime(dst) < os.path.getmtime(src)):
            todo.append((src, dst))
    if not todo:
        return
    session = None
    for src, dst in todo:
        img = Image.open(src).convert("RGBA")
        stem = os.path.splitext(os.path.basename(src))[0]
        method = "model" if stem in MODEL_ONLY else cut_method(img)
        if method == "transparent":
            out = img
        else:
            if session is None:
                from rembg import new_session
                session = new_session("isnet-general-use")
            from rembg import remove
            out = remove(img, session=session)
            if method == "white":
                # keep a pixel if either the model or the white flood says it is
                # product - light-coloured parts survive, the white backdrop goes
                from PIL import ImageChops
                keep = ImageChops.lighter(out.getchannel("A"), flood_white(img).getchannel("A"))
                out = img.copy()
                out.putalpha(keep)
        # drop faint halo pixels, then trim to the object
        alpha = out.getchannel("A").point(lambda a: 0 if a < 24 else a)
        out.putalpha(alpha)
        box = alpha.getbbox()
        if box:
            out = out.crop(box)
        dst = dst[:-4] + ".webp"
        out.save(dst, "WEBP", quality=90, method=6)
        print(f"cut-out ({method}) ->", dst)


def cut_method(img):
    """transparent: already cut out. white: studio shot on white - flood the
    background from the edges, which keeps white parts of the product intact.
    model: anything else goes through rembg."""
    import numpy as np
    a = np.asarray(img)
    if (a[..., 3] < 10).mean() > 0.05:
        return "transparent"
    border = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])[:, :3]
    if (border.min(axis=1) > 225).mean() > 0.9:
        return "white"
    return "model"


def flood_white(img):
    import cv2
    import numpy as np
    rgb = np.asarray(img.convert("RGB"))
    h, w = rgb.shape[:2]
    # pad with white so the fill reaches every edge pixel from one seed
    pad = cv2.copyMakeBorder(rgb, 2, 2, 2, 2, cv2.BORDER_CONSTANT, value=(255, 255, 255))
    mask = np.zeros((h + 6, w + 6), np.uint8)
    # fixed range: compare against pure white, so the fill cannot creep
    # along a soft gradient into a light grey product
    cv2.floodFill(pad, mask, (0, 0), (0, 0, 0), (10, 10, 10), (0, 0, 0),
                  cv2.FLOODFILL_MASK_ONLY | cv2.FLOODFILL_FIXED_RANGE | (255 << 8) | 8)
    bg = mask[3:-3, 3:-3]
    alpha = 255 - bg
    alpha = cv2.GaussianBlur(alpha, (3, 3), 0)
    out = img.copy()
    out.putalpha(Image.fromarray(alpha))
    return out


def cutout_path(stem):
    for ext in (".webp", ".png"):
        p = os.path.join(CUT_DIR, stem + ext) if stem else ""
        if p and os.path.exists(p):
            return p
    return None


# Hugo wants print and web to show the AM-R055 register image; storage-layouts'
# pages use the same file. Fall back to our own cut-out until it is filed.
REGISTER_IMG = os.path.join(CUT_DIR, "{am}_OEM.webp")   # the register photo, copied by build.py


def item_image(it):
    am = it["register_no"]
    if am.startswith("AM"):
        p = REGISTER_IMG.format(am=am)
        if os.path.exists(p):
            return p
    return cutout_path(it["image"])


# ------------------------------------------------------------- QR codes
QR_PATH = os.path.join(DATA_DIR, "qr.json")


def load_qr():
    """{"cabinet": url, "shelves": {"1.1": url, ...}} from storage-layouts."""
    import json
    if not os.path.exists(QR_PATH):
        return {}
    with open(QR_PATH, encoding="utf-8") as f:
        return json.load(f)


def draw_qr(c, url, x, y, size):
    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    w = QrCodeWidget(url, barBorder=0)
    x0, y0, x1, y1 = w.getBounds()
    d = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), 0, 0])
    d.add(w)
    renderPDF.draw(d, c, x, y)


# ------------------------------------------------------------- helpers
def draw_logo(c, x, y, h):
    img = Image.open(LOGO)
    w = h * img.width / img.height
    img.close()
    c.drawImage(LOGO, x, y, width=w, height=h, mask="auto")
    return w


def fit_image(c, path, x, y, w, h):
    """Draw centred in the box, aspect kept, anchored to the box bottom."""
    from reportlab.lib.utils import ImageReader
    im = Image.open(path).convert("RGBA")
    box = im.getchannel("A").getbbox()      # register images carry padding
    if box:
        im = im.crop(box)
    iw, ih = im.size
    s = min(w / iw, h / ih)
    dw, dh = iw * s, ih * s
    # 300 dpi at printed size is plenty; full-size photos made a 47 MB file
    target = int(max(dw, dh) / 72 * 300)
    if max(iw, ih) > target:
        im.thumbnail((target, target), Image.LANCZOS)
    c.drawImage(ImageReader(im), x + (w - dw) / 2, y, width=dw, height=dh, mask="auto")


def wrap_to_width(c, text, font, size, width, max_lines):
    words, lines, cur = text.split(), [], ""
    for wd in words:
        trial = (cur + " " + wd).strip()
        if c.stringWidth(trial, font, size) <= width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while c.stringWidth(lines[-1] + "...", font, size) > width and lines[-1]:
            lines[-1] = lines[-1][:-1]
        lines[-1] = lines[-1].rstrip() + "..."
    return lines


# ------------------------------------------------------- page 1: layout
def layout_page(c, tree):
    W, H = landscape(A3)
    M = 10 * mm
    header_h = 30 * mm

    # header: logo left, title centred, subtitle under
    hy = H - M - header_h
    draw_logo(c, M, hy + 5 * mm, 10 * mm)
    c.setFillColor(DEEP_SEA_BLUE)
    c.setFont("Helvetica-Bold", 26)
    c.drawCentredString(W / 2, hy + header_h / 2 + 1 * mm, TITLE)
    c.setFillColor(TECHNOLOGY_BLUE)
    c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(W / 2, hy + header_h / 2 - 6 * mm, SUBTITLE)
    c.setStrokeColor(TECHNOLOGY_BLUE)
    c.setLineWidth(2)
    c.line(M, hy, W - M, hy)

    # QR to the full cabinet layout page - URL supplied by storage-layouts
    url = load_qr().get("cabinet")
    if url:
        q = 26 * mm
        qy = hy + 2 * mm
        draw_qr(c, url, W - M - q, qy, q)
        tx = W - M - q - 4 * mm
        c.setFillColor(TECHNOLOGY_BLUE)
        c.setFont("Helvetica-Bold", 13)
        c.drawRightString(tx, qy + q / 2 + 1 * mm, "SCAN FOR THE")
        c.drawRightString(tx, qy + q / 2 - 4.5 * mm, "FULL LAYOUT")

    bins = list(tree)
    gap = 5 * mm
    top = hy - 4 * mm
    bottom = M
    bin_w = (W - 2 * M - gap * (len(bins) - 1)) / len(bins)
    band_h = 8 * mm
    shelves = max(len(tree[b]) for b in bins)
    shelf_h = (top - bottom - band_h) / shelves

    for bi, b in enumerate(bins):
        bx = M + bi * (bin_w + gap)
        # bin band
        c.setFillColor(DEEP_SEA_BLUE)
        c.roundRect(bx, top - band_h, bin_w, band_h, 1.5 * mm, fill=1, stroke=0)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 14)
        c.drawCentredString(bx + bin_w / 2, top - band_h / 2 - 14 * 0.35, f"CABINET {b}")

        # bin frame
        c.setStrokeColor(RULE)
        c.setLineWidth(1)
        c.rect(bx, bottom, bin_w, top - band_h - bottom, fill=0, stroke=1)

        for si, s in enumerate(tree[b]):
            sy = top - band_h - (si + 1) * shelf_h
            if si % 2 == 1:
                c.setFillColor(PANEL_BG)
                c.rect(bx, sy, bin_w, shelf_h, fill=1, stroke=0)
            if si:
                c.setStrokeColor(RULE)
                c.line(bx, sy + shelf_h, bx + bin_w, sy + shelf_h)
            slots = tree[b][s]
            n = max(slots)               # slot count = highest position on that shelf
            slot_w = bin_w / n
            for p in range(1, n + 1):
                sx = bx + (p - 1) * slot_w
                if p > 1:
                    c.setStrokeColor(RULE)
                    c.setLineWidth(0.6)
                    c.line(sx, sy + 2 * mm, sx, sy + shelf_h - 2 * mm)
                draw_slot(c, sx, sy, slot_w, shelf_h, f"{b}.{s}.{p}", slots.get(p, []))


def draw_slot(c, x, y, w, h, loc, items):
    pad = 1.6 * mm
    text_h = 17 * mm
    img_box_h = h - text_h - 2 * pad

    # location code - the thing people match to the shelf label
    c.setFillColor(TECHNOLOGY_BLUE)
    c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(x + w / 2, y + text_h - 4.2 * mm, loc)

    if not items:
        c.setFillColor(STEEL_GREY)
        c.setFont("Helvetica-Oblique", 7)
        c.drawCentredString(x + w / 2, y + text_h - 8.5 * mm, "Free slot")
        return

    # photos side by side when items share a slot; identical items show one photo
    by_desc = OrderedDict()
    for it in items:
        if item_image(it):
            by_desc.setdefault(it["description"], item_image(it))
    paths = list(by_desc.values())
    if paths:
        cw = (w - 2 * pad) / len(paths)
        for i, pth in enumerate(paths):
            fit_image(c, pth, x + pad + i * cw + 0.8 * mm, y + text_h + pad,
                      cw - 1.6 * mm, img_box_h)

    # register numbers + description
    regs = [it["register_no"] for it in items if it["register_no"] not in ("", "-")]
    descs = list(OrderedDict.fromkeys(it["description"] for it in items))
    c.setFillColor(DEEP_SEA_BLUE)
    reg_line = "  /  ".join(regs) if regs else ""
    size = 7.5
    while reg_line and c.stringWidth(reg_line, "Helvetica-Bold", size) > w - 2 * pad and size > 5:
        size -= 0.25
    c.setFont("Helvetica-Bold", size)
    if reg_line:
        c.drawCentredString(x + w / 2, y + text_h - 8 * mm, reg_line)

    desc = " + ".join(descs)
    max_lines = 3
    dsize = 5.8
    # shrink a shared slot's long description rather than cut it off
    while dsize > 4.6 and len(wrap_to_width(c, desc, "Helvetica", dsize, w - 2 * pad, 99)) > max_lines:
        dsize -= 0.2
    lines = wrap_to_width(c, desc, "Helvetica", dsize, w - 2 * pad, max_lines)
    c.setFont("Helvetica", dsize)
    first = y + text_h - (11 * mm if reg_line else 8 * mm)
    for i, ln in enumerate(lines):
        c.drawCentredString(x + w / 2, first - i * (dsize + 1), ln)


# ------------------------------------------------------- page 2: register
def register_page(c, items):
    W, H = A3
    M = 18 * mm
    rows = [it for it in items if it["description"]]

    hy = H - M - 16 * mm
    draw_logo(c, M, hy + 2 * mm, 11 * mm)
    c.setFillColor(DEEP_SEA_BLUE)
    c.setFont("Helvetica-Bold", 20)
    c.drawRightString(W - M, hy + 8 * mm, TITLE)
    c.setFillColor(TECHNOLOGY_BLUE)
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(W - M, hy + 2.5 * mm, SUBTITLE)

    cols = [("Location", 34 * mm), ("Register No", 38 * mm),
            ("Description", W - 2 * M - 72 * mm)]
    head_h = 10 * mm
    table_top = hy - 6 * mm
    row_h = min(7.6 * mm, (table_top - head_h - M) / len(rows))

    c.setFillColor(DEEP_SEA_BLUE)
    c.rect(M, table_top - head_h, W - 2 * M, head_h, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 12)
    cx = M
    for name, cw in cols:
        if name == "Description":
            c.drawString(cx + 4 * mm, table_top - head_h / 2 - 12 * 0.35, name)
        else:
            c.drawCentredString(cx + cw / 2, table_top - head_h / 2 - 12 * 0.35, name)
        cx += cw

    fs = min(11.5, row_h / mm * 1.45)
    for i, it in enumerate(rows):
        ry = table_top - head_h - (i + 1) * row_h
        if i % 2 == 0:
            c.setFillColor(PANEL_BG)
            c.rect(M, ry, W - 2 * M, row_h, fill=1, stroke=0)
        base = ry + row_h / 2 - fs * 0.35
        c.setFillColor(DEEP_SEA_BLUE)
        c.setFont("Helvetica-Bold", fs)
        c.drawCentredString(M + cols[0][1] / 2, base, it["bin_location"])
        c.setFont("Helvetica", fs)
        c.drawCentredString(M + cols[0][1] + cols[1][1] / 2, base, it["register_no"] or "-")
        c.drawString(M + cols[0][1] + cols[1][1] + 4 * mm, base, it["description"])

    # grid
    c.setStrokeColor(RULE)
    c.setLineWidth(0.6)
    t_bottom = table_top - head_h - len(rows) * row_h
    c.rect(M, t_bottom, W - 2 * M, table_top - t_bottom, fill=0, stroke=1)
    cx = M
    for _, cw in cols[:-1]:
        cx += cw
        c.line(cx, t_bottom, cx, table_top)


# ------------------------------------- bay labels (99.1 x 38.1 mm, 14 per A4)
# One per bay, C.S.P = cabinet.shelf.bay (Hugo 13/11/2026); the QR opens that bay on its shelf page.
STICKER_PDF = os.path.normpath(os.path.join(
    OUT_DIR, "lab_equipment_storage_bay_labels_L7073.pdf"))
LABEL_W, LABEL_H = 99.1 * mm, 38.1 * mm
L_LEFT, L_TOP, L_HGAP = 4.65 * mm, 15.15 * mm, 2.5 * mm
X_OFFSET, Y_OFFSET = -0.5 * mm, 1 * mm


def draw_sticker(c, x, y, bay, url):
    b, s, p_ = bay.split(".")
    c.saveState()
    p = c.beginPath()
    p.rect(x, y, LABEL_W, LABEL_H)
    c.clipPath(p, stroke=0)   # no outline: die-cut edges never line up with a printed one (Hugo 13/11/2026)

    pad = 3.5 * mm
    q = LABEL_H - 2 * pad
    draw_qr(c, url, x + LABEL_W - pad - q, y + pad, q)

    # text block centred vertically in the space left of the QR
    tx = x + pad + 1 * mm
    cy = y + LABEL_H / 2
    c.setFillColor(TECHNOLOGY_BLUE)
    c.setFont("Helvetica-Bold", 30)
    c.drawString(tx, cy + 1.5 * mm, bay)
    c.setFillColor(DEEP_SEA_BLUE)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(tx, cy - 4.2 * mm - 10 * 0.35, f"CABINET {b}  ·  SHELF {s}  ·  BAY {p_}")
    c.setFillColor(STEEL_GREY)
    c.setFont("Helvetica", 7)
    c.drawString(tx, cy - 9.6 * mm - 7 * 0.35, "Lab Equipment Storage  -  scan for this bay")
    draw_logo(c, tx, y + LABEL_H - pad - 3.6 * mm, 3.6 * mm)
    c.restoreState()


def shelf_stickers(qr):
    shelves = qr.get("bays", {})
    if not shelves:
        return None
    keys = sorted(shelves, key=lambda k: tuple(int(v) for v in k.split(".")))
    W, H = A4
    c = canvas.Canvas(STICKER_PDF, pagesize=A4)
    c.setTitle("Lab Equipment Storage bay labels")
    for i, k in enumerate(keys):
        slot = i % 14
        if i and slot == 0:
            c.showPage()
        row, col = divmod(slot, 2)
        px = L_LEFT + col * (LABEL_W + L_HGAP) + X_OFFSET
        py = H - L_TOP - (row + 1) * LABEL_H + Y_OFFSET
        draw_sticker(c, px, py, k, shelves[k])
    c.save()
    print("bay labels ->", STICKER_PDF)
    return STICKER_PDF


# ------------------------- small bay labels, Avery L7651 (38.1 x 21.2 mm, 65 per A4)
SMALL_BAY_PDF = os.path.normpath(os.path.join(
    OUT_DIR, "lab_equipment_storage_bay_labels_L7651.pdf"))
S_W, S_H = 38.1 * mm, 21.2 * mm
S_LEFT, S_TOP, S_HGAP = 5.75 * mm, 10.7 * mm, 2.0 * mm


def draw_small_bay(c, x, y, bay, url):
    b, s, p_ = bay.split(".")
    c.saveState()
    pth = c.beginPath()
    pth.rect(x, y, S_W, S_H)
    c.clipPath(pth, stroke=0)
    pad = 2 * mm
    q = S_H - 2 * pad
    draw_qr(c, url, x + pad, y + pad, q)
    tx = x + pad + q + 1.8 * mm
    top = y + S_H - pad
    draw_logo(c, tx, top - 2 * mm, 2 * mm)
    c.setFillColor(TECHNOLOGY_BLUE)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(tx, top - 7.6 * mm, bay)
    c.setFillColor(DEEP_SEA_BLUE)
    c.setFont("Helvetica-Bold", 5.6)
    for i, line in enumerate((f"CABINET {b}", f"SHELF {s}", f"BAY {p_}")):
        c.drawString(tx, top - 10.6 * mm - i * 2.3 * mm, line)
    c.restoreState()


def small_bay_labels(qr):
    bays = qr.get("bays", {})
    if not bays:
        return None
    keys = sorted(bays, key=lambda k: tuple(int(v) for v in k.split(".")))
    W, H = A4
    c = canvas.Canvas(SMALL_BAY_PDF, pagesize=A4)
    c.setTitle("Lab Equipment Storage bay labels (small)")
    for i, k in enumerate(keys):
        slot = i % 65
        if i and slot == 0:
            c.showPage()
        row, col = divmod(slot, 5)
        draw_small_bay(c, S_LEFT + col * (S_W + S_HGAP), H - S_TOP - (row + 1) * S_H, k, bays[k])
    c.save()
    print("small bay labels ->", SMALL_BAY_PDF)
    return SMALL_BAY_PDF


# ------------------------------------------------ asset tags (Avery L7073)
# QR = the asset's storage-layouts page (specs, cal cert, "Send it on" into
# EDT); Code 128 = the plain AM##### that every printed tag already carries.
# QR target agreed with tag-form 05/11/2026.
ASSET_TAG_PDF = os.path.normpath(os.path.join(
    OUT_DIR, "lab_equipment_storage_asset_tags_L7073.pdf"))
ASSET_LINKS = os.path.join(DATA_DIR, "asset_links.csv")   # page URL per AM, from build.py
LAYOUT = os.path.join(DATA_DIR, "layout.json")            # register fields per AM (serials)


def draw_asset_tag(c, x, y, it, reg, url, border=False, w=None, h=None):
    w, h = w or LABEL_W, h or LABEL_H
    from reportlab.graphics.barcode import code128
    am = it["register_no"]
    c.saveState()
    p = c.beginPath()
    p.rect(x, y, w, h)
    c.clipPath(p, stroke=0)
    if border:
        c.setStrokeColor(HexColor("#D8DEE6"))
        c.setLineWidth(0.5)
        c.rect(x + 0.6 * mm, y + 0.6 * mm, w - 1.2 * mm, h - 1.2 * mm, fill=0, stroke=1)

    # Hugo 05/11/2026: the asset photo where the QR was, a smaller QR top right
    pad = 3.2 * mm
    box = h - 2 * pad
    img = item_image(it)
    if img:
        fit_image(c, img, x + pad, y + pad, box, box)

    tx = x + pad + box + 3 * mm
    right = x + w - pad
    tw = right - tx
    top = y + h - pad
    qs = 16 * mm                      # ~0.55 mm modules: scans on a phone at arm's length
    draw_qr(c, url, right - qs, top - qs, qs)
    hw = tw - qs - 2 * mm             # text width beside the QR

    # logo row with the cabinet location under it
    draw_logo(c, tx, top - 3.8 * mm, 3.8 * mm)
    c.setFillColor(TECHNOLOGY_BLUE)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(tx, top - 8.4 * mm, it["bin_location"])
    # Hugo 13/11/2026: Indication Only instruments say so on the tag (AM-R055 CalibrationStatus)
    if ((reg or {}).get("CalibrationStatus") or "").strip().lower() == "indication only":
        bx = tx + c.stringWidth(it["bin_location"], "Helvetica-Bold", 10) + 2.5 * mm
        label = "INDICATION ONLY"
        bw, bh = c.stringWidth(label, "Helvetica-Bold", 6.2) + 3 * mm, 3.6 * mm
        by = top - 8.4 * mm - 0.9 * mm
        c.setFillColor(DEEP_SEA_BLUE)
        c.roundRect(bx, by, bw, bh, bh / 2, fill=1, stroke=0)
        c.setFillColor(HexColor("#FFFFFF"))
        c.setFont("Helvetica-Bold", 6.2)
        c.drawCentredString(bx + bw / 2, by + (bh - 6.2 * 0.7) / 2, label)

    c.setFillColor(DEEP_SEA_BLUE)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(tx, top - 13.6 * mm, am)

    desc = it["description"]
    size = 7
    while size > 5.6 and len(wrap_to_width(c, desc, "Helvetica-Bold", size, hw, 99)) > 2:
        size -= 0.2
    c.setFont("Helvetica-Bold", size)
    for i, ln in enumerate(wrap_to_width(c, desc, "Helvetica-Bold", size, hw, 2)):
        c.drawString(tx, top - 17.6 * mm - i * (size + 1), ln)

    serial = (reg or {}).get("SerialNo") or ""
    sub = f"S/N {serial}" if serial else ""
    if sub:
        c.setFillColor(STEEL_GREY)
        c.setFont("Helvetica", 6.5)
        c.drawString(tx, y + pad + 7.6 * mm, sub)

    bc = code128.Code128(am, barWidth=0.26 * mm, barHeight=6 * mm, quiet=False)
    bc.drawOn(c, tx, y + pad)
    c.restoreState()


def asset_tags(items, qr):
    if not os.path.exists(ASSET_LINKS):
        return None
    with open(ASSET_LINKS, newline="", encoding="utf-8-sig") as f:
        links = {r["asset_no"]: r["asset_page_url"] for r in csv.DictReader(f)}
    import json
    reg = {}
    if os.path.exists(LAYOUT):
        reg = {a["AssetNumber"]: a for a in json.load(open(LAYOUT, encoding="utf-8"))["assets"]}
    seen, tags = set(), []
    for it in items:
        am = it["register_no"]
        # tag only numbers that are in AM-R055 and have a page
        # the export keeps a "NOT IN AM-R055" row with blank fields for missing numbers
        in_register = am in reg
        if am.startswith("AM") and am not in seen and in_register and links.get(am):
            seen.add(am)
            tags.append(it)
    W, H = A4
    c = canvas.Canvas(ASSET_TAG_PDF, pagesize=A4)
    c.setTitle("Lab Equipment Storage asset tags")
    for i, it in enumerate(tags):
        slot = i % 14
        if i and slot == 0:
            c.showPage()
        row, col = divmod(slot, 2)
        px = L_LEFT + col * (LABEL_W + L_HGAP) + X_OFFSET
        py = H - L_TOP - (row + 1) * LABEL_H + Y_OFFSET
        am = it["register_no"]
        draw_asset_tag(c, px, py, it, reg.get(am), links[am])
    c.save()
    print(f"asset tags ({len(tags)}) ->", ASSET_TAG_PDF)
    asset_tags_a3(tags, reg, links)
    return ASSET_TAG_PDF


# Hugo 05/11/2026: the same tags laid out on A3 and cut by hand. Landscape, 4 x 7 butted on
# one cut grid (28 a sheet), tags the L7073 size 99.1 x 38.1 mm. Tried 132 x 60 mm: Hugo said
# too big, keep this size. Crop marks run into the margin.
ASSET_TAG_A3_PDF = os.path.normpath(os.path.join(
    OUT_DIR, "lab_equipment_storage_asset_tags_A3.pdf"))
A3_COLS, A3_ROWS = 4, 7
A3_TAG_W, A3_TAG_H = LABEL_W, LABEL_H
A3_SCALE = 1


def asset_tags_a3(tags, reg, links):
    W, H = landscape(A3)
    cw, ch = A3_TAG_W * A3_SCALE, A3_TAG_H * A3_SCALE
    gw, gh = A3_COLS * cw, A3_ROWS * ch
    x0, y0 = (W - gw) / 2, (H - gh) / 2
    per = A3_COLS * A3_ROWS
    c = canvas.Canvas(ASSET_TAG_A3_PDF, pagesize=(W, H))
    c.setTitle("Lab Equipment Storage asset tags (A3)")
    for start in range(0, len(tags), per):
        if start:
            c.showPage()
        page = tags[start:start + per]
        for i, it in enumerate(page):
            row, col = divmod(i, A3_COLS)
            am = it["register_no"]
            c.saveState()
            c.translate(x0 + col * cw, y0 + gh - (row + 1) * ch)
            c.scale(A3_SCALE, A3_SCALE)
            draw_asset_tag(c, 0, 0, it, reg.get(am), links[am], border=False, w=A3_TAG_W, h=A3_TAG_H)
            c.restoreState()
        rows = -(-len(page) // A3_COLS)
        c.setStrokeColor(HexColor("#9AA0A6"))
        c.setLineWidth(0.3)
        top, bottom = y0 + gh, y0 + gh - rows * ch
        for k in range(A3_COLS + 1):            # cut lines through the used rows, crop marks above and below
            x = x0 + k * cw
            c.line(x, bottom, x, top)
            c.line(x, top + 2 * mm, x, top + 8 * mm)
            c.line(x, bottom - 8 * mm, x, bottom - 2 * mm)
        for r in range(rows + 1):
            y = top - r * ch
            c.line(x0, y, x0 + gw, y)
            c.line(x0 - 8 * mm, y, x0 - 2 * mm, y)
            c.line(x0 + gw + 2 * mm, y, x0 + gw + 8 * mm, y)
    c.save()
    print(f"asset tags A3 ({len(tags)}) ->", ASSET_TAG_A3_PDF)

# ------------------------------------------------------------------ main
def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    items = load_items()
    make_cutouts(items)
    tree = build_tree(items)
    c = canvas.Canvas(OUT_PDF, pagesize=landscape(A3))
    c.setTitle("Lab Equipment Storage")
    layout_page(c, tree)
    c.showPage()
    c.setPageSize(A3)
    register_page(c, items)
    c.save()
    shelf_stickers(load_qr())
    small_bay_labels(load_qr())
    asset_tags(items, load_qr())
    missing =[it["bin_location"] + " " + it["description"] for it in items
               if it["description"] and not cutout_path(it["image"])]
    print("pdf ->", OUT_PDF)
    if missing:
        print("no photo yet:", *missing, sep="\n  ")


if __name__ == "__main__":
    main()
