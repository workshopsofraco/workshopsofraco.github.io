#!/usr/bin/env python3
"""
Storage layouts: one web page per storage location (cabinet, shelving bay, store...),
one per shelf and one per asset, plus the QR codes that point at them.

This file lives in the site repository (workshopsofraco/workshopsofraco.github.io) and
runs in two places:
  - GitHub Actions, on every push of data/*/layout.json (the flow commits one after each
    change made on a page): rebuilds the pages and the printable PDFs.
  - Hugo's PC, for refreshes and checks.

Where an asset sits is held in the Asset Register (AM-R055): Storage Location (which
storage) and Storage Bay (Cabinet.Shelf.Position, e.g. 2.3.3; several assets may share
a bay). Bays that exist, including empty ones, are in the SharePoint list
"SLB - Storage Layout Bays". data/<slug>/layout.json is the snapshot of both.
Locations without the register (fastener shelves) still use data/<slug>/items.csv.

  python tools/build.py                 build the site + QR codes
  python tools/build.py --refresh       first snapshot the register, bays, transit state
                                        and photos (signed-in browser, PC only)
  python tools/build.py --publish       build, then commit and push
  python tools/build.py --check         every live page 200 + every QR decodes to it
"""

import argparse
import csv
import datetime as dt
import html
import re
import json
import os
import shutil
import subprocess
import sys
from collections import OrderedDict

import qrcode
from PIL import Image

TOOLS = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.dirname(TOOLS)                 # the repository root is the published site
DATA = os.path.join(SITE, "data")
REGISTRY = os.path.join(DATA, "locations.json")
LOGO = os.path.join(DATA, "logo_full.png")
IMAGES = os.path.join(DATA, "images")
SIGNERS = os.path.join(DATA, "signers.json")
WORKSPACE = os.environ.get("SOFRACO_WORKSPACE",
                           os.path.join(os.environ.get("USERPROFILE", ""), r"OneDrive\B.R.A.I.N\AI"))
FSL_SITE = "https://sofraco.sharepoint.com/sites/Purchasing"
FSL_LIST = "a229ce0f-cd0b-402d-9c21-c9101f4a56ba"
BAY_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")

esc = html.escape


def ws(path):
    return os.path.join(SITE, *path.split("/"))


def ldir(loc, *parts):
    return os.path.join(DATA, loc["slug"], *parts)


# ------------------------------------------------------------------ data
def load_layout(loc):
    p = ldir(loc, "layout.json")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {"bays": [], "assets": []}


def asset_title(a):
    """Make + model + register description, without repeating words already there."""
    desc = (a.get("Desc2") or "").strip()
    lead = [x for x in (a.get("Make"), a.get("Model")) if x and x.lower() not in desc.lower()]
    return " ".join(lead + [desc]).strip() or a.get("AssetNumber", "")


def bay_key(bay):
    return [int(x) for x in bay.split(".")]


def load_items(loc):
    if loc.get("asset_register"):
        lay = load_layout(loc)
        fb = {}
        p = ldir(loc, "image_fallback.json")
        if os.path.exists(p):
            fb = json.load(open(p, encoding="utf-8"))
        rows, used = [], set()
        for a in lay.get("assets", []):
            am = (a.get("AssetNumber") or "").strip().upper()
            bay = (a.get("StorageBay") or "").strip()
            rows.append({"bin_location": bay if BAY_RE.match(bay) else "", "ref": am, loc["ref_col"]: am,
                         "description": asset_title(a), "make": a.get("Make") or "", "model": a.get("Model") or "",
                         "image": fb.get(am, ""), "_reg": a, "transit": a.get("transit")})
            used.add(bay)
        for bay in lay.get("bays", []):
            if BAY_RE.match(bay) and bay not in used:
                rows.append({"bin_location": bay, "ref": "", "description": ""})
        return rows
    with open(ws(loc["csv"]), newline="", encoding="utf-8-sig") as f:
        rows = [{k: (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]
    for r in rows:
        r["ref"] = r.get(loc["ref_col"], "")
    return rows


def build_tree(items):
    """({bin: {shelf: {pos: [items]}}}, [unplaced items]) in numeric order."""
    tree, unplaced = {}, []
    for it in items:
        if not it.get("bin_location"):
            if it.get("description"):
                unplaced.append(it)
            continue
        b, s, p = (int(x) for x in it["bin_location"].split("."))
        tree.setdefault(b, {}).setdefault(s, {}).setdefault(p, [])
        if it.get("description"):
            tree[b][s][p].append(it)
    ordered = OrderedDict()
    for b in sorted(tree):
        ordered[b] = OrderedDict()
        for s in sorted(tree[b]):
            ordered[b][s] = OrderedDict((p, tree[b][s].get(p, [])) for p in range(1, max(tree[b][s]) + 1))
    return ordered, unplaced


def shelf_key(b, s):
    return f"{b}.{s}"


def shelf_slug(b, s):
    return f"{b}-{s}"


def url_for(base, loc, b=None, s=None):
    u = base + loc["slug"] + "/"
    return u if b is None else u + shelf_slug(b, s) + "/"


# ---------------------------------------------------------------- images
def find_image(loc, stem, ref=""):
    """data/images/<AM>_OEM.* (the Asset Register photo) first, then the location's own photo."""
    names = ([ref + "_OEM"] if ref else []) + ([stem] if stem else [])
    for n in names:
        for ext in (".webp", ".png", ".jpg", ".jpeg"):
            p = os.path.join(IMAGES, n + ext)
            if os.path.exists(p):
                return p
    return None


def export_image(loc, stem, ref=""):
    """Web copy (max 640px webp) into <slug>/img/. Returns the file name."""
    src = find_image(loc, stem, ref)
    if not src:
        return None
    name = os.path.splitext(os.path.basename(src))[0].replace(" ", "_") + ".webp"
    dst = os.path.join(SITE, loc["slug"], "img", name)
    if not os.path.exists(dst) or os.path.getmtime(dst) < os.path.getmtime(src):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        im = Image.open(src)
        im = im.convert("RGBA") if im.mode in ("P", "LA", "RGBA") else im.convert("RGB")
        im.thumbnail((640, 640))
        im.save(dst, "WEBP", quality=82)
    return name


def fastener_svg(it):
    """Line drawing for a fastener with no photo (ported from Bay Bolt Buddy)."""
    cat = (it.get("category") or it.get("description", "")).lower()
    stroke = 'stroke="currentColor" fill="none" stroke-width="3"'
    if "washer" in cat:
        body = f'<circle cx="100" cy="60" r="42" {stroke}/><circle cx="100" cy="60" r="16" {stroke}/>'
    elif "nut" in cat:
        body = (f'<polygon points="58,60 79,24 121,24 142,60 121,96 79,96" {stroke}/>'
                f'<circle cx="100" cy="60" r="18" {stroke}/>')
    else:
        try:
            length = float(it.get("length_mm") or 40)
        except ValueError:
            length = 40
        shank = max(40, min(150, length * 1.4))
        x0 = 100 - (shank + 26) / 2
        head = "" if "rod" in cat else f'<rect x="{x0:.0f}" y="34" width="26" height="52" {stroke}/>'
        sx = x0 + (0 if "rod" in cat else 26)
        threads = "".join(f'<line x1="{x:.0f}" y1="46" x2="{x + 4:.0f}" y2="74" stroke="currentColor" stroke-width="1.2" opacity=".5"/>'
                          for x in range(int(sx + 6), int(sx + shank - 4), 6))
        body = head + f'<rect x="{sx:.0f}" y="46" width="{shank:.0f}" height="28" {stroke}/>' + threads
    return f'<svg viewBox="0 0 200 120" class="draw" role="img" aria-label="{esc(it.get("description", ""))}">{body}</svg>'


# ------------------------------------------------------------------ html
CSS = """
:root{--sh:0 1px 2px rgba(5,18,51,.10),0 3px 10px rgba(5,18,51,.08);--deep:#051233;--blue:#2878F5;--grey:#828C96;--panel:#F2F7FF;--rule:#C9D6EA;--ink:#0B1220;--red:#C62828;--bg:#fff}
*{box-sizing:border-box}[hidden]{display:none!important}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.35}
.mono{font-family:'JetBrains Mono',ui-monospace,Consolas,monospace}
header{position:sticky;top:0;z-index:5;background:rgba(255,255,255,.96);border-bottom:1px solid var(--rule)}
.hd{max-width:1180px;margin:0 auto;padding:10px 16px;display:flex;align-items:center;justify-content:space-between;gap:12px}
.hd img{height:26px;width:auto}
.hd .t{text-align:right;min-width:0}
.hd h1{margin:0;font-size:20px;font-weight:800;letter-spacing:-.02em;color:var(--deep);text-transform:uppercase}
.hd .sub{font-size:10px;letter-spacing:.14em;color:var(--grey);text-transform:uppercase}
main{max-width:1180px;margin:0 auto;padding:18px 16px 40px}
.crumb{font-size:12px;margin:0 0 14px}.crumb a{color:var(--blue);text-decoration:none}
.sec{display:flex;justify-content:space-between;align-items:baseline;border-bottom:2px solid var(--deep);padding-bottom:6px;margin:26px 0 12px;gap:8px}
.sec h2{margin:0;font-size:12px;letter-spacing:.18em;text-transform:uppercase;font-weight:600}
.sec span{font-size:11px;color:var(--grey)}
.bins{display:grid;gap:16px}
@media(min-width:900px){.bins{grid-template-columns:repeat(var(--n),1fr)}}
.bin{border-radius:14px;overflow:hidden;background:#fff;box-shadow:0 2px 4px rgba(5,18,51,.06),0 8px 24px rgba(5,18,51,.10)}
.binhd{background:var(--deep);color:#fff;font-weight:700;letter-spacing:.12em;padding:8px 12px;text-transform:uppercase;font-size:13px}
.shelf{padding:10px}.shelf+.shelf{border-top:1px solid #E8EEF6}
.shelf>a.sh{display:flex;justify-content:space-between;align-items:center;text-decoration:none;color:var(--deep);font-weight:700;font-size:13px;margin:0 2px 6px}
.shelf>a.sh small{color:var(--blue);font-weight:600}
.slots{display:grid;grid-template-columns:repeat(var(--p),minmax(0,1fr));gap:6px}
.slot{background:var(--panel);border-radius:10px;padding:6px;display:flex;flex-direction:column;align-items:center;text-align:center;min-width:0}
.slot .ph{height:64px;width:100%;display:flex;align-items:flex-end;justify-content:center}
.slot .ph img{max-height:64px;max-width:100%;object-fit:contain}
.code{color:var(--blue);font-weight:800}
.slot .code{font-size:14px;margin-top:4px}
.ref{font-weight:700;font-size:11px}
.desc{font-size:10.5px;color:#333;overflow-wrap:break-word;hyphens:auto}
.free{color:var(--grey);font-size:11px;letter-spacing:.1em;text-transform:uppercase;margin:auto 0}
.draw{width:100%;height:100%;color:var(--deep)}
.slot .draw{height:64px}
/* shelf page */
.strip{display:grid;grid-template-columns:repeat(var(--p),1fr);gap:6px;border-radius:12px;padding:8px;background:var(--panel)}
.strip a{display:block;text-align:center;padding:10px 0;border-radius:8px;box-shadow:var(--sh);background:#fff;color:var(--deep);text-decoration:none;font-weight:800}
.strip a.empty{box-shadow:none;background:transparent;color:var(--grey)}
.cards{display:grid;gap:12px}
@media(min-width:700px){.cards{grid-template-columns:repeat(2,1fr)}}
.card{border-radius:12px;background:#fff;box-shadow:var(--sh);padding:14px;display:flex;gap:14px;align-items:center;scroll-margin-top:70px}
.card .ph{flex:0 0 120px;height:120px;display:flex;align-items:center;justify-content:center}
.card .ph img{max-width:120px;max-height:120px;object-fit:contain}
.card .code{font-size:24px;line-height:1}
.card .ref{font-size:15px;margin-top:4px}
.card .desc{font-size:14px;margin-top:4px}
.card .mm{font-size:12px;color:var(--grey);margin-top:4px}
.metrics{display:flex;gap:16px;margin-top:8px}
.metrics div{display:flex;flex-direction:column}.metrics b{font-size:18px}.metrics span{font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--grey)}
.low b,.low span{color:var(--red)}
.card.free{box-shadow:none;background:var(--panel);justify-content:center;color:var(--grey)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{background:var(--deep);color:#fff;text-align:left;padding:6px 8px;font-weight:600;font-size:11px;letter-spacing:.08em;text-transform:uppercase}
td{border-bottom:1px solid var(--rule);padding:6px 8px;vertical-align:top}
tr:nth-child(even) td{background:var(--panel)}
td.c{white-space:nowrap}td a{color:var(--blue);font-weight:700;text-decoration:none}
.note{border-radius:10px;border:0;background:var(--panel);padding:12px;font-size:13px}
.idx a{display:flex;justify-content:space-between;border-radius:12px;box-shadow:var(--sh);padding:14px;margin-bottom:8px;text-decoration:none;color:var(--deep);font-weight:700}
.plain{text-decoration:none;color:inherit}.slot>a.plain{display:flex;flex-direction:column;align-items:center;width:100%}
.slot .ref a{color:var(--ink);text-decoration:none}
.more{font-size:12px;color:var(--blue);font-weight:600;margin-top:8px}
.hero{display:flex;gap:18px;align-items:center;flex-wrap:wrap}
.hero .ph{flex:0 0 200px;height:200px;display:flex;align-items:center;justify-content:center}
.hero .ph img{max-width:200px;max-height:200px;object-fit:contain}
.hero .code{font-size:30px}.hero .desc{font-size:17px;margin-top:6px}
dl.spec{display:grid;grid-template-columns:max-content 1fr;margin:0;border-radius:12px;overflow:hidden;box-shadow:var(--sh)}
dl.spec dt,dl.spec dd{margin:0;padding:8px 10px;border-bottom:1px solid var(--rule)}
dl.spec dt{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--grey);background:var(--panel)}
.badge{display:inline-block;padding:2px 8px;font-size:12px;font-weight:700;border-radius:999px;background:var(--panel);color:var(--deep)}
.badge.ok{background:#E3F4E8;color:#1B6B34}.badge.bad{background:#FDE7E7;color:var(--red)}
a.btn,button.btn{display:block;text-align:center;background:var(--deep);color:#fff;text-decoration:none;font-weight:700;padding:14px;margin-top:10px;border-radius:10px}
a.btn.alt,button.btn.alt{background:#fff;color:var(--deep);border:1.5px solid var(--deep)}
.hint{font-size:11px;color:var(--grey);margin-top:6px}
.mv{border-radius:12px;background:var(--panel);padding:12px}
.mv label{display:block;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--grey);margin-top:10px}
.mv select,.mv input{display:block;width:100%;font:inherit;font-size:16px;padding:10px;margin-top:4px;border:1px solid var(--rule);border-radius:8px;background:#fff;color:var(--ink);text-transform:none;letter-spacing:0}
.mv canvas{display:block;width:100%;height:160px;background:#fff;border:1px dashed var(--rule);border-radius:8px;margin-top:4px;touch-action:none}
.mv button.btn{width:100%;border:0;font:inherit;font-weight:700;cursor:pointer}.mv button.btn.alt{border:1.5px solid var(--deep)}
.mvmsg{font-size:14px;font-weight:600;color:var(--deep)}.mvmsg.bad{color:var(--red)}
.mvroute{font-size:15px;font-weight:700;margin-top:6px}
.slot .mini{display:flex;flex-direction:column;align-items:center;width:100%;text-decoration:none;color:inherit;padding:6px 3px;border:0;border-radius:8px;background:#fff;box-shadow:var(--sh);margin-top:6px}
.slot .mini .ref{font-size:11px;margin-top:4px}
.mini.transit,.card.transit{background:#FFF4E5;box-shadow:0 0 0 2px #F59E0B,var(--sh)}
.tr{font-size:10.5px;font-weight:800;color:#B45309;margin-top:4px;text-transform:uppercase;letter-spacing:.04em}
.card .tr{font-size:12px}
.note.transit{background:#FFF4E5;border-color:#F59E0B;color:#7C2D12;margin-top:12px}
.mv .gate .btn{margin-top:0}.ed{display:none}
body.editing .ed{display:inline-block}
.editbar{display:flex;align-items:center;gap:12px;margin:0 0 10px;flex-wrap:wrap}.editbar .btn{margin:0;padding:10px 18px;width:auto;font:inherit;font-weight:700;cursor:pointer}.slot .ed{white-space:nowrap}
body.editing .slot{box-shadow:inset 0 0 0 1px var(--blue)}
body.editing .mini{cursor:grab;touch-action:none;user-select:none;-webkit-user-select:none}
.mini.dragging{opacity:.3}.mini.pending{opacity:.55}
.ghost .ph{height:64px;display:flex;align-items:flex-end;justify-content:center}.ghost .ph img{max-height:64px;max-width:100%;object-fit:contain}
.ghost .desc{font-size:10.5px}.mini.added{outline:2px dashed var(--blue);outline-offset:-2px}.mini.bad{box-shadow:0 0 0 2px var(--red),var(--sh)}.mini.bad .desc{color:var(--red)}
.ghost{position:fixed;z-index:50;pointer-events:none;opacity:.9;box-shadow:0 8px 24px rgba(5,18,51,.35);background:#fff;border-radius:8px}
.slot.over,#trash.over{background:#E3EEFF;outline:2px solid var(--blue)}
.slot .ed{font:inherit;font-size:11px;font-weight:700;color:var(--blue);background:#fff;border:1px solid var(--blue);padding:3px 8px;border-radius:6px;margin:4px 2px 0;cursor:pointer}
.slot .rmbay,.slot .rmasset{color:var(--red);border-color:var(--red)}.slot:not(.empty) .rmbay{display:none!important}
.shelf .addbay{font:inherit;font-size:11px;font-weight:700;color:var(--blue);background:#fff;border:1px solid var(--blue);padding:3px 8px;border-radius:6px;margin-left:auto;margin-right:8px}
#trash{position:sticky;bottom:10px;z-index:20;margin:10px 0;padding:14px;text-align:center;font-weight:700;color:var(--red);
  background:#FFF1F1;border:2px dashed var(--red);border-radius:12px}
body.editing #trash{display:block}
details.minor{margin-top:22px;border-top:1px solid var(--rule);padding-top:10px}
details.minor summary{cursor:pointer;color:var(--blue);font-size:13px;font-weight:600}
details.minor .mv{margin-top:10px}
footer{max-width:1180px;margin:0 auto;padding:14px 16px 30px;border-top:1px solid var(--rule);font-size:11px;color:var(--grey);display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap}
"""


def page(title, h1, sub, body, depth, updated):
    up = "../" * depth
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} - Sofraco</title><meta name="robots" content="noindex">
<link rel="icon" href="{up}logo.png">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body>
<header><div class="hd"><img src="{up}logo.png" alt="Sofraco"><div class="t"><h1>{esc(h1)}</h1><div class="sub mono">{esc(sub)}</div></div></div></header>
<main>{body}</main>
<footer><span>Sofraco Engineering Systems</span><span class="mono">Updated {updated}</span></footer>
</body></html>"""


def photo(loc, it, cls="ph"):
    name = export_image(loc, it.get("image"), it.get("ref", ""))
    if name:
        return f'<div class="{cls}"><img src="img/{esc(name)}" alt="{esc(it["description"])}" loading="lazy"></div>'
    if loc.get("source") == "fsl":
        return f'<div class="{cls}">{fastener_svg(it)}</div>'
    return f'<div class="{cls}"></div>'


def metrics(it):
    out = []
    if it.get("qty"):
        low = it.get("reorder_point") and it["qty"].replace(".", "", 1).isdigit() and \
            float(it["qty"]) <= float(it["reorder_point"] or 0)
        out.append(f'<div class="{"low" if low else ""}"><b>{esc(it["qty"])}</b><span>On hand</span></div>')
    if it.get("reorder_point"):
        out.append(f'<div><b>{esc(it["reorder_point"])}</b><span>Reorder pt</span></div>')
    return f'<div class="metrics mono">{"".join(out)}</div>' if out else ""


def cabinet_body(loc, tree, unplaced, items):
    n_items = sum(1 for i in items if i.get("description"))
    body = []
    if tree:
        body.append(f'<div class="sec"><h2 class="mono">01 / Layout</h2><span class="mono">{len(tree)} {loc["unit_word"].lower()}s · {n_items} items</span></div>')
        if live(loc) and loc.get("asset_register"):
            body.append('<div data-editor-only hidden><div class="editbar"><button id="editbtn" class="btn alt" type="button">Edit layout</button>'
                        '<button id="editcancel" class="btn alt" type="button" hidden>Cancel</button>'
                        '<span id="editst" class="mvmsg"></span></div>'
                        '<div id="trash" class="ed">Drop here to remove from this storage</div></div>')
        body.append(f'<div class="bins" id="layout" style="--n:{len(tree)}">')
        for b, shelves in tree.items():
            body.append(f'<section class="bin"><div class="binhd">{loc["unit_word"]} {b}</div>')
            for s, slots in shelves.items():
                body.append(f'<div class="shelf" data-next="{b}.{s}.{max(slots) + 1 if slots else 1}"><a class="sh" href="../{loc["slug"]}/{shelf_slug(b, s)}/">'
                            f'<span>Shelf {b}.{s}</span><button class="ed addbay" type="button">+ bay</button><small>Open shelf ›</small></a>'
                            f'<div class="slots" style="--p:{len(slots)}">')
                for p, its in slots.items():
                    body.append(slot_cell(loc, b, s, p, its))
                body.append('</div></div>')
            body.append('</section>')
        body.append('</div>')
    if unplaced:
        body.append(f'<div class="sec"><h2 class="mono">{"02" if tree else "01"} / Not yet placed</h2><span class="mono">{len(unplaced)} items</span></div>')
        body.append('<div class="note">These items are on the list but have no shelf position yet. '
                    'Once each one is given a position (bay.shelf.slot) it appears on the layout above and on its shelf page.</div>')
        body.append(register_table(loc, unplaced, link=False))
    if tree and live(loc) and loc.get("asset_register"):
        body.append(f'<script>{EDIT_JS}</script>')
    if tree:
        placed = [i for i in items if i.get("bin_location") and i.get("description")]
        body.append(f'<div class="sec"><h2 class="mono">{"03" if unplaced else "02"} / Register</h2><span class="mono">{len(placed)} rows</span></div>')
        body.append(register_table(loc, placed, link=True))
    return "".join(body)


def item_href(loc, it, b, s, p, prefix):
    """Asset page when the location has one, else the item on its shelf page."""
    if loc.get("asset_register") and it.get("ref"):
        return f'{prefix}asset/{it["ref"]}/'
    return f'{prefix}{shelf_slug(b, s)}/#p{p}' if prefix == "" else f"#p{p}"


def transit_note(tr):
    """Hugo 05/11/2026: an asset sent on but not yet signed for shows in colour."""
    if not tr:
        return ""
    return f'<div class="tr">In transit → {esc(tr.get("to") or "?")} · not signed yet</div>'


def mini_card(loc, it, b, s, p):
    href = item_href(loc, it, b, s, p, "")
    return (f'<a class="mini{" transit" if it.get("transit") else ""}" href="{href}" data-asset="{esc(it["ref"])}">{photo(loc, it)}'
            f'<div class="ref mono">{esc(it["ref"])}</div><div class="desc">{esc(it["description"])}</div>'
            f'{transit_note(it.get("transit"))}<button class="ed rmasset" type="button">Remove</button></a>')


def slot_cell(loc, b, s, p, its):
    """Hugo 05/11/2026: each asset in a bay is its own card with its photo."""
    code = f"{b}.{s}.{p}"
    head = (f'<div class="code mono">{code}</div><button class="ed addhere" type="button" title="Add an asset to {code}">+ asset</button>'
            f'<button class="ed rmbay" type="button" title="Remove bay {code}">Remove bay</button>')
    if not its:
        return f'<div class="slot empty" data-bay="{code}">{head}<div class="free">Free slot</div></div>'
    return f'<div class="slot" data-bay="{code}">{head}{"".join(mini_card(loc, i, b, s, p) for i in its)}</div>'


def register_table(loc, rows, link):
    extra_qty = any(r.get("qty") for r in rows)
    head = f'<tr><th>Location</th><th>{esc(loc["ref_label"])}</th><th>Description</th>' + \
        ("<th>Qty</th>" if extra_qty else "<th>Make / Model</th>") + "</tr>"
    out = [f'<table>{head}']
    for r in rows:
        locn = r.get("bin_location") or "-"
        if link and r.get("bin_location"):
            b, s, p = r["bin_location"].split(".")
            locn = f'<a href="../{loc["slug"]}/{b}-{s}/#p{p}">{esc(locn)}</a>'
        last = esc(r.get("qty", "")) if extra_qty else esc(" ".join(x for x in (r.get("make"), r.get("model")) if x))
        ref = esc(r["ref"])
        if loc.get("asset_register") and r["ref"]:
            ref = f'<a href="asset/{ref}/">{ref}</a>'
        out.append(f'<tr><td class="c mono">{locn}</td><td class="c mono">{ref}</td>'
                   f'<td>{esc(r["description"])}</td><td>{last}</td></tr>')
    out.append("</table>")
    return "".join(out)


def shelf_body(loc, b, s, slots):
    body = [f'<p class="crumb mono"><a href="../">‹ {esc(loc["title"])}</a></p>',
            f'<div class="sec"><h2 class="mono">01 / Shelf positions</h2><span class="mono">{loc["unit_word"]} {b} · Shelf {s}</span></div>',
            f'<div class="strip" style="--p:{len(slots)}">']
    for p, its in slots.items():
        body.append(f'<a class="mono{"" if its else " empty"}" href="#p{p}">{b}.{s}.{p}</a>')
    body.append('</div>')
    count = sum(len(v) for v in slots.values())
    body.append(f'<div class="sec"><h2 class="mono">02 / What belongs here</h2><span class="mono">{count} items</span></div><div class="cards">')
    for p, its in slots.items():
        code = f"{b}.{s}.{p}"
        if not its:
            body.append(f'<div class="card free" id="p{p}"><span class="code mono">{code}</span>&nbsp; Free slot</div>')
        for k, it in enumerate(its):
            mm = " ".join(x for x in (it.get("make"), it.get("model")) if x)
            linked = loc.get("asset_register") and it["ref"]
            tag, href = ("a", f' href="../asset/{esc(it["ref"])}/"') if linked else ("article", "")
            body.append(f'<{tag} class="card plain{" transit" if it.get("transit") else ""}"{href}{f" id=p{p}" if k == 0 else ""}>{photo(loc, it)}<div>'
                        f'<div class="code mono">{code}</div><div class="ref mono">{esc(loc["ref_label"])}: {esc(it["ref"] or "-")}</div>'
                        f'<div class="desc">{esc(it["description"])}</div>'
                        f'{f"<div class=mm>{esc(mm)}</div>" if mm else ""}{metrics(it)}'
                        f'{transit_note(it.get("transit"))}'
                        f'{"<div class=more>Specifications &amp; calibration ›</div>" if linked else ""}</div></{tag}>')
    body.append('</div>')
    body.append(shelf_panel(loc, b, s, slots))
    return "".join(body)


SP = "https://sofraco.sharepoint.com"
MONTHS = {m: i for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}


def cert_date(name):
    """Sort key for a certificate file name: EXP_dd-mm-yyyy, yyyy-MON-dd or yyyy-mm-dd."""
    import re
    m = re.search(r"(\d{2})[-_.](\d{2})[-_.](\d{4})", name)
    if m:
        return (int(m[3]), int(m[2]), int(m[1]))
    m = re.search(r"(\d{4})[-_.]([A-Za-z]{3})[-_.](\d{1,2})", name)
    if m and m[2].upper() in MONTHS:
        return (int(m[1]), MONTHS[m[2].upper()], int(m[3]))
    m = re.search(r"(\d{4})[-_.](\d{2})[-_.](\d{1,2})", name)
    return (int(m[1]), int(m[2]), int(m[3])) if m else (0, 0, 0)


def quote_url(text):
    from urllib.parse import quote
    return quote(text, safe="")


def au_date(iso):
    if not iso:
        return ""
    d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(dt.timezone(dt.timedelta(hours=11)))
    return d.strftime("%d/%m/%Y")


MSAL_CDN = "https://cdn.jsdelivr.net/npm/@azure/msal-browser@4/lib/msal-browser.min.js"

# Signed-in calls to tag-form's flow "EDT - Asset Page Actions". Every page that changes
# anything loads this once; panels call SL.post({...}).
SL_JS = """
window.SL=(function(){
var cfg=document.getElementById('slcfg').dataset,SCOPE=['https://service.flow.microsoft.com//User'],app=null,acct=null;
function go(u){window.location.href=u;return false}
function token(){
  if(window.MV_TEST_TOKEN)return Promise.resolve(window.MV_TEST_TOKEN);
  return app.acquireTokenSilent({scopes:SCOPE,account:acct}).then(function(r){return r.accessToken})
   .catch(function(){return app.acquireTokenRedirect({scopes:SCOPE,account:acct,onRedirectNavigate:go})})}
function post(o){return token().then(function(t){return fetch(cfg.flow,{method:'POST',
  headers:{'Content-Type':'application/json','Authorization':'Bearer '+t},body:JSON.stringify(o)})})
  .then(function(r){if(r.status===401||r.status===403)throw new Error('auth');return r.json()})}
function signIn(){return app.loginRedirect({scopes:SCOPE,onRedirectNavigate:go})}
function errText(e){return e&&e.message==='auth'?'Your sign-in was not accepted. Sign in with your Sofraco account.':'Could not reach the tracker. Nothing was changed.'}
var ready=new Promise(function(res){
  if(window.MV_TEST_TOKEN){res(true);return}
  app=new msal.PublicClientApplication({auth:{clientId:cfg.client,authority:'https://login.microsoftonline.com/'+cfg.tenant,
    redirectUri:cfg.redirect},cache:{cacheLocation:'localStorage'}});
  app.initialize().then(function(){return app.handleRedirectPromise()}).then(function(r){
    acct=(r&&r.account)||app.getAllAccounts()[0]||null;if(acct)app.setActiveAccount(acct);res(!!acct)})
  .catch(function(){res(false)})});
function gate(panel,onIn){ready.then(function(ok){panel.querySelector('.inner').hidden=!ok;if(ok&&onIn)onIn()})}
// layout editing (bays) shows only for the editors in locations.json
function isEditor(){var u=(window.MV_TEST_USER||(acct&&acct.username)||'').toLowerCase();
  return !!u&&(','+(cfg.editors||'')+',').indexOf(','+u+',')>=0}
function whenDom(f){if(document.readyState!=='loading')f();else document.addEventListener('DOMContentLoaded',f)}
whenDom(function(){var gb=document.getElementById('gatebtn');if(gb)gb.onclick=function(){gb.disabled=true;signIn()};
  ready.then(function(ok){var g=document.getElementById('gated'),s=document.getElementById('gatescreen');
    if(g)g.hidden=!ok;if(s)s.hidden=ok;
    [].forEach.call(document.querySelectorAll('[data-editor-only]'),function(e){e.hidden=!(ok&&isEditor())})})});
// Hugo 06/11/2026: pick from a dropdown; a box to type in appears only for "Other..."
function pick(selId){var s=document.getElementById(selId),o=document.getElementById(selId+'x');
  if(s&&o)s.addEventListener('change',function(){o.hidden=s.value!=='__other';if(!o.hidden)o.focus()});
  return function(){return s.value==='__other'?o.value.trim():s.value}}
return {post:post,signIn:signIn,ready:ready,gate:gate,errText:errText,pick:pick,isEditor:isEditor,location:cfg.location};})();
"""


def sl_head(loc):
    """Config + MSAL + SL helper for a page that changes things (asset and shelf pages)."""
    mv = loc.get("move") or {}
    return (f'<div id="slcfg" hidden data-flow="{esc(mv["flow_url"])}" data-client="{esc(mv["client_id"])}"'
            f' data-tenant="{esc(mv["tenant_id"])}" data-redirect="{esc(loc["_base"] + "auth/")}"'
            f' data-location="{esc(loc["title"])}" data-editors="{esc(",".join(mv.get("editors", [])).lower())}"></div>'
            f'<script src="{MSAL_CDN}"></script><script>{SL_JS}</script>')


def live(loc):
    mv = loc.get("move") or {}
    return bool(mv.get("flow_url") and mv.get("client_id"))


GATE = ""   # the page itself is behind the one sign-in (gated)

GATE_SCREEN = ('<div id="gatescreen" class="mv" hidden><div class="mvmsg">Sofraco staff only</div>'
               '<p class="hint">Sign in once with your own Sofraco account. This phone will remember you.</p>'
               '<button id="gatebtn" class="btn" type="button">Sign in with Sofraco</button></div>')


FRESH_JS = """
(function(){var f=document.getElementById('fresh');if(!f)return;var g=f.dataset.gen,src=f.dataset.src,busy=false;
function check(){if(busy||window.SLediting||document.querySelector('.ghost'))return;
  fetch(src+'?t='+Date.now(),{cache:'no-store'}).then(function(r){return r.json()}).then(function(d){
    if(d.generatedAt&&d.generatedAt>g){busy=true;var u=new URL(location.href);u.searchParams.set('v',Date.parse(d.generatedAt));location.replace(u.toString())}})
  .catch(function(){})}
check();
window.SLfresh=function(){var n=0,iv=setInterval(function(){if(++n>30){clearInterval(iv);return}check()},8000)};
document.addEventListener('visibilitychange',function(){if(!document.hidden)check()});
})();
"""


def fresh(loc):
    if not loc.get("asset_register"):
        return ""
    gen = load_layout(loc).get("generatedAt", "")
    return (f'<div id="fresh" hidden data-gen="{esc(gen)}" data-src="{esc(loc["_base"] + "data/" + loc["slug"] + "/layout.json")}"></div>'
            f'<script>{FRESH_JS}</script>')


def gated(loc, body):
    """Whole page shown only after Sofraco sign-in."""
    if not live(loc):
        return body
    return sl_head(loc) + GATE_SCREEN + f'<div id="gated" hidden>{body}</div>' + fresh(loc)


MOVE_JS = """
(function(){
var box=document.getElementById('move');if(!box)return;
var A=box.dataset.asset,st=document.getElementById('mvst');
function msg(t,bad){st.textContent=t;st.className='mvmsg'+(bad?' bad':'')}
function show(id){['mvsend','mvdel'].forEach(function(x){document.getElementById(x).hidden=(x!==id)})}
function load(){msg('Checking where it is…');SL.post({action:'status',asset:A}).then(function(r){
  if(r.ok===false){msg(r.message||'Not available.',1);return}
  if(!r.found){msg('This asset was not found in the register.',1);return}
  var who=r.signedInAs?' · signed in as '+r.signedInAs:'';
  if(r.inTransit){document.getElementById('mvroute').textContent=(r.from||'?')+' → '+(r.to||'?')+(r.isr?' · '+r.isr:'');show('mvdel');msg('In transit - sign below when it arrives'+who)}
  else{show('mvsend');msg('Currently at: '+(r.whereabouts||'unknown')+who)}
 }).catch(function(e){msg(SL.errText(e),1)})}
var to=SL.pick('mvto'),recv=SL.pick('mvrecv'),PLACES=['GI','SCM','Workshop','Calibration Facility'];
document.getElementById('mvgo').onclick=function(){
  var v=to(),k=PLACES.indexOf(v)>=0?v:null;
  if(!v){msg('Choose where it is going.',1);return}
  var b=this;b.disabled=true;msg('Sending…');
  SL.post(k?{action:'send',asset:A,to:k}:{action:'send',asset:A,to:'Other',toOther:v.slice(0,100)}).then(function(r){b.disabled=false;
   if(r.ok){msg('Sent'+(r.isr?' - '+r.isr:'')+'. It now shows as in transit.');setTimeout(load,1500);if(window.SLfresh)SLfresh()}else msg(r.message||'Not sent.',1)})
  .catch(function(e){b.disabled=false;msg(SL.errText(e),1)})};
var c=document.getElementById('mvsig'),x=c.getContext('2d'),drawn=false,dn=false;
function fit(){var r=c.getBoundingClientRect();if(!r.width)return;c.width=r.width*2;c.height=r.height*2;x.setTransform(2,0,0,2,0,0);x.lineWidth=2.2;x.lineCap='round';x.strokeStyle='#051233';drawn=false}
function pt(e){var r=c.getBoundingClientRect();return[e.clientX-r.left,e.clientY-r.top]}
c.onpointerdown=function(e){dn=true;c.setPointerCapture(e.pointerId);var p=pt(e);x.beginPath();x.moveTo(p[0],p[1])};
c.onpointermove=function(e){if(!dn)return;var p=pt(e);x.lineTo(p[0],p[1]);x.stroke();drawn=true};
c.onpointerup=function(){dn=false};
document.getElementById('mvclear').onclick=fit;
document.getElementById('mvdeliver').onclick=function(){
  if(!drawn){msg('Please sign before confirming the delivery.',1);return}
  var b=this;b.disabled=true;msg('Recording delivery…');
  SL.post({action:'deliver',asset:A,name:recv().slice(0,80),signature:c.toDataURL('image/png')}).then(function(r){b.disabled=false;
   if(r.ok){msg('Delivered. Thank you.');fit();setTimeout(load,1500);if(window.SLfresh)SLfresh()}else msg(r.message||'Not recorded.',1)})
  .catch(function(e){b.disabled=false;msg(SL.errText(e),1)})};
new ResizeObserver(fit).observe(c);show('');
SL.gate(box,load);
})();
"""

# Hugo 05/11/2026: from an asset, move it to another bay (or take it off the layout);
# the flow writes Storage Bay in the Asset Register and republishes.
BAY_JS = """
(function(){
var box=document.getElementById('bay');if(!box)return;
var A=box.dataset.asset,st=document.getElementById('bayst'),bay=SL.pick('baynew');
function msg(t,bad){st.textContent=t;st.className='mvmsg'+(bad?' bad':'')}
function done(r){if(r.ok){msg('Saved. This page refreshes itself in about a minute.'+(r.warning?' Note: '+r.warning:''));if(window.SLfresh)SLfresh()}
  else msg(r.message||'Not changed.',1)}
document.getElementById('baymove').onclick=function(){
  var v=bay();
  if(!/^\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}$/.test(v)){msg(v?'A bay is Cabinet.Shelf.Bay, for example 2.3.3.':'Choose the bay.',1);return}
  var b=this;b.disabled=true;msg('Moving…');
  SL.post({action:'move',asset:A,bay:v,location:SL.location}).then(function(r){b.disabled=false;done(r)})
  .catch(function(e){b.disabled=false;msg(SL.errText(e),1)})};
document.getElementById('bayremove').onclick=function(){
  if(!confirm('Take '+A+' off this storage layout? It stays in the Asset Register.'))return;
  var b=this;b.disabled=true;msg('Removing…');
  SL.post({action:'remove',asset:A}).then(function(r){b.disabled=false;done(r)})
  .catch(function(e){b.disabled=false;msg(SL.errText(e),1)})};
SL.gate(box);
})();
"""

# Hugo 05/11/2026: add assets to a bay by asset number (several per bay are fine),
# add bays and remove empty ones - straight from the shelf page.
SHELF_JS = """
(function(){
var box=document.getElementById('shelfedit');if(!box)return;
var st=document.getElementById('shst'),pickBay=SL.pick('shbay');
function msg(t,bad){st.textContent=t;st.className='mvmsg'+(bad?' bad':'')}
function done(r){if(r.ok){msg('Saved. This page refreshes itself in about a minute.'+(r.warning?' Note: '+r.warning:''));if(window.SLfresh)SLfresh()}
  else msg(r.message||'Not changed.',1)}
function am(v){v=v.trim().toUpperCase().replace(/\\s+/g,'');if(/^\\d{1,5}$/.test(v))v='AM'+('00000'+v).slice(-5);return v}
function bayOk(v){return /^\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}$/.test(v)}
document.getElementById('shadd').onclick=function(){
  var a=am(document.getElementById('shasset').value),bay=pickBay();
  if(!/^AM\\d{5}$/.test(a)){msg('Type the asset number, for example AM00869 or just 869.',1);return}
  if(!bayOk(bay)){msg(bay?'A bay is Cabinet.Shelf.Bay, for example '+box.dataset.first+'.':'Choose the bay.',1);return}
  var b=this;b.disabled=true;msg('Adding '+a+'…');
  SL.post({action:'place',location:SL.location,bay:bay,asset:a}).then(function(r){b.disabled=false;done(r)})
  .catch(function(e){b.disabled=false;msg(SL.errText(e),1)})};
document.getElementById('shnewbay').onclick=function(){
  var b=this,bay=box.dataset.next;b.disabled=true;msg('Adding bay '+bay+'…');
  SL.post({action:'addBay',location:SL.location,bay:bay}).then(function(r){b.disabled=false;done(r)})
  .catch(function(e){b.disabled=false;msg(SL.errText(e),1)})};
[].forEach.call(box.querySelectorAll('.shrm'),function(btn){btn.onclick=function(){
  var bay=btn.dataset.bay;if(!confirm('Remove empty bay '+bay+'?'))return;btn.disabled=true;msg('Removing bay '+bay+'…');
  SL.post({action:'removeBay',location:SL.location,bay:bay}).then(function(r){btn.disabled=false;done(r)})
  .catch(function(e){btn.disabled=false;msg(SL.errText(e),1)})}});
SL.gate(box);
})();
"""


def dropdown(i, label, options, other, placeholder, first=None, numeric=False):
    """A real dropdown (tap and pick); "Other..." reveals a box to type in."""
    head = first or ("", "Choose…")
    opts = f'<option value="{esc(head[0])}">{esc(head[1])}</option>' + \
        "".join(f'<option value="{esc(o)}">{esc(o)}</option>' for o in options) + \
        f'<option value="__other">{esc(other)}</option>'
    return (f'<label>{esc(label)}<select id="{i}">{opts}</select></label>'
            f'<input id="{i}x" hidden maxlength="100" autocomplete="off" placeholder="{esc(placeholder)}"'
            f'{" inputmode=decimal" if numeric else ""}>')


# Hugo 06/11/2026: rearrange the layout on the cabinet page itself - drag an asset card onto
# another bay, + to add an asset (or a bay), drag onto Remove to take it off. Pointer events,
# so a finger on a phone and a mouse on a PC both work. Every change goes through the flow.
EDIT_JS = """
(function(){
// Hugo 06/11/2026: "It must be instant". Edits show on screen at once; Done saves them all in one
// request; what was saved stays on screen across reloads until the rebuilt page catches up; and the
// page lines itself up with the live register as soon as it opens.
var root=document.getElementById('layout'),btn=document.getElementById('editbtn'),cancel=document.getElementById('editcancel');
if(!root||!window.SL)return;
var st=document.getElementById('editst'),trash=document.getElementById('trash'),on=false,drag=null,ops=[];
var fresh=document.getElementById('fresh'),GEN=fresh?fresh.dataset.gen:'';
function msg(t,bad){if(!st)return;st.textContent=t;st.className='mvmsg'+(bad?' bad':'')}
function am(v){v=(v||'').trim().toUpperCase().replace(/\\s+/g,'');if(/^\\d{1,5}$/.test(v))v='AM'+('00000'+v).slice(-5);return v}
function esc(s){return String(s||'').replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
// ---------------- the layout on screen
function slot(bay){return root.querySelector('.slot[data-bay="'+bay+'"]')}
function card(A){return root.querySelector('.mini[data-asset="'+A+'"]')}
function fixEmpty(s){if(s&&!s.querySelector('.mini')){s.classList.add('empty');if(!s.querySelector('.free')){var f=document.createElement('div');f.className='free';f.textContent='Free slot';s.appendChild(f)}}}
function fill(s){var f=s.querySelector('.free');if(f)f.remove();s.classList.remove('empty')}
function shelfOf(bay){var p=bay.split('.');return[].filter.call(root.querySelectorAll('.shelf'),function(sh){var n=(sh.dataset.next||'').split('.');return n[0]===p[0]&&n[1]===p[1]})[0]}
function addBay(bay){if(slot(bay))return slot(bay);var sh=shelfOf(bay);if(!sh)return null;var p=bay.split('.');
  var ns=document.createElement('div');ns.className='slot empty';ns.dataset.bay=bay;
  ns.innerHTML='<div class="code mono">'+bay+'</div><button class="ed addhere" type="button">+ asset</button><button class="ed rmbay" type="button">Remove bay</button><div class="free">Free slot</div>';
  var row=sh.querySelector('.slots'),after=[].filter.call(row.children,function(c){return +c.dataset.bay.split('.')[2]<+p[2]}).pop();
  if(after)after.after(ns);else row.prepend(ns);row.style.setProperty('--p',row.children.length);
  var n=sh.dataset.next.split('.');if(+p[2]>=+n[2])sh.dataset.next=p[0]+'.'+p[1]+'.'+(+p[2]+1);return ns}
function dropBay(bay){var s=slot(bay);if(!s||s.querySelector('.mini'))return;var row=s.parentNode;s.remove();row.style.setProperty('--p',row.children.length)}
function newCard(A,desc,pending){var c=document.createElement('a');c.className='mini'+(pending?' added':'');c.dataset.asset=A;c.href='asset/'+A+'/';
  c.innerHTML='<div class="ph"></div><div class="ref mono">'+esc(A)+'</div><div class="desc">'+esc(desc||'')+'</div><button class="ed rmasset" type="button">Remove</button>';
  photoInto(c,A);return c}
var RAW='https://raw.githubusercontent.com/workshopsofraco/workshopsofraco.github.io/main/data/images/';
function photoInto(c,A,data){var ph=c.querySelector('.ph');if(!ph||ph.querySelector('img'))return;var img=document.createElement('img');img.alt=A;
  var srcs=(data?[data]:[]).concat(['img/'+A+'_OEM.webp',RAW+A+'_OEM.webp',RAW+A+'_OEM.png']),i=0;
  img.onerror=function(){i++;if(i<srcs.length)img.src=srcs[i];else img.remove()};img.src=srcs[0];ph.appendChild(img)}
function details(A){var c=card(A);if(!c)return;
  SL.post({action:'status',asset:A}).then(function(r){var c2=card(A);if(!c2)return;
    if(r&&r.found===false){c2.classList.add('bad');c2.querySelector('.desc').textContent='Not in the Asset Register';
      msg(A+' is not in the Asset Register - it will not be saved.',1);ops=ops.filter(function(o){return o.asset!==A});return}
    var d=[r.Make,r.Model,r.Desc2].filter(Boolean).join(' ')||r.description;if(d)c2.querySelector('.desc').textContent=d;
    if(r.photo){var old=c2.querySelector('.ph img');if(old)old.remove();photoInto(c2,A,r.photo)}}).catch(function(){})}
function put(A,bay,desc,pending){var s=slot(bay)||addBay(bay);if(!s)return;var c=card(A)||newCard(A,desc,pending),from=c.parentNode;
  if(from!==s){fill(s);s.appendChild(c);if(from&&from.classList&&from.classList.contains('slot'))fixEmpty(from)}}
function take(A){var c=card(A);if(!c)return;var s=c.parentNode;c.remove();fixEmpty(s)}
// ---------------- the page always opens on the last layout this phone saw (no flicker on refresh),
// then checks the live register in the background and only changes what is different.
var VIEW='sl-view:'+location.pathname.replace(/index\\.html$/,'');
function transitOn(c,to){c.classList.toggle('transit',!!to);var tn=c.querySelector('.tr');if(tn)tn.remove();
  if(to){var d=document.createElement('div');d.className='tr';d.textContent='In transit → '+to+' · not signed yet';c.insertBefore(d,c.querySelector('.ed'))}}
function applyLayout(r){
  (r.bays||[]).forEach(addBay);var keep={};
  (r.assets||[]).forEach(function(a){if(!/^\\d+\\.\\d+\\.\\d+$/.test(a.StorageBay||''))return;keep[a.AssetNumber]=1;
    put(a.AssetNumber,a.StorageBay,a.desc||[a.Make,a.Model,a.Desc2].filter(Boolean).join(' '));
    var c=card(a.AssetNumber);if(c)transitOn(c,a.transitTo||(a.transit&&(a.transit.to||'?')))});
  [].forEach.call(root.querySelectorAll('.mini'),function(c){if(!keep[c.dataset.asset])take(c.dataset.asset)});
  var bays={};(r.bays||[]).forEach(function(b){bays[b]=1});
  [].forEach.call(root.querySelectorAll('.slot.empty'),function(s){if(!bays[s.dataset.bay])dropBay(s.dataset.bay)})}
function snapshot(){try{var v={at:new Date().toISOString(),bays:[],assets:[]};
  [].forEach.call(root.querySelectorAll('.slot'),function(s){v.bays.push(s.dataset.bay)});
  [].forEach.call(root.querySelectorAll('.mini'),function(c){var tn=c.querySelector('.tr');
    v.assets.push({AssetNumber:c.dataset.asset,StorageBay:c.closest('.slot').dataset.bay,desc:c.querySelector('.desc').textContent,
      transitTo:tn?tn.textContent.replace(/^In transit → | · not signed yet$/g,''):null})});
  localStorage.setItem(VIEW,JSON.stringify(v))}catch(e){}}
(function(){try{var v=JSON.parse(localStorage.getItem(VIEW)||'null');if(v&&(!GEN||v.at>GEN))applyLayout(v)}catch(e){}})();
function live(){SL.post({action:'layout',location:SL.location}).then(function(r){
  if(!r||!r.ok||!r.assets||(on&&ops.length))return;applyLayout(r);snapshot()}).catch(function(){})}
SL.ready.then(function(ok){if(ok)live()});
if(!btn)return;
// ---------------- editing
function count(){msg(ops.length?ops.length+' change'+(ops.length>1?'s':'')+' not saved yet - press Done to save.':'Drag an asset onto another bay. + asset adds one, + bay adds a bay.')}
function setOn(v){on=v;window.SLediting=v;document.body.classList.toggle('editing',on);btn.textContent=on?'Done':'Edit layout';cancel.hidden=!on;
  try{sessionStorage.setItem('sl-edit',on?'1':'')}catch(e){}if(on)count();else if(!ops.length)msg('')}
function rec(op){
  if(op.action==='move'||op.action==='remove'){var prev=ops.filter(function(o){return o.asset===op.asset})[0];
    if(prev&&prev.action==='place'){if(op.action==='remove')ops.splice(ops.indexOf(prev),1);else prev.bay=op.bay;count();return}
    ops=ops.filter(function(o){return o.asset!==op.asset})}
  if(op.action==='removeBay'){var added=ops.filter(function(o){return o.action==='addBay'&&o.bay===op.bay})[0];
    if(added){ops.splice(ops.indexOf(added),1);count();return}}
  ops.push(op);count()}
function finish(n,fails,done){ops=[];btn.disabled=false;cancel.disabled=false;setOn(false);
  [].forEach.call(root.querySelectorAll('.mini.added'),function(c){c.classList.remove('added')});snapshot();
  if(fails.length)msg((n-fails.length)+' saved, '+fails.length+' not saved: '+fails.join(' / '),1);else msg('All '+n+' change'+(n>1?'s':'')+' saved.');
  if(window.SLfresh)SLfresh()}
function one(todo,n,fails,done){if(!todo.length)return finish(n,fails,done);var o=todo.shift();
  msg('Saving '+(n-todo.length)+' of '+n+'…');var q=Object.assign({location:SL.location},o);
  SL.post(q).then(function(r){if(r.ok)done.push(o);else fails.push(r.message||o.action);if(r.warning)fails.push(r.warning);one(todo,n,fails,done)})
    .catch(function(e){fails.push(SL.errText(e));one(todo,n,fails,done)})}
function save(){
  if(!ops.length){setOn(false);return}
  var list=ops.map(function(o){var c={action:o.action};if(o.asset)c.asset=o.asset;if(o.bay)c.bay=o.bay;return c}),n=list.length;
  btn.disabled=true;cancel.disabled=true;msg('Saving '+n+' change'+(n>1?'s':'')+'…');
  // one request for everything; the flow commits once. Falls back to one at a time if batch is not there.
  SL.post({action:'batch',location:SL.location,ops:list}).then(function(r){
    if(r&&r.results){var fails=[],done=[];r.results.forEach(function(x){if(x.ok)done.push(list[x.i]);else fails.push(x.message||list[x.i].action)});
      if(r.warning)fails.push(r.warning);finish(n,fails,done)}
    else if(r&&r.ok)finish(n,[],list)   // more than 20 changes: the flow accepts them and saves after replying
    else if(r&&r.message&&!/unknown|action/i.test(r.message))finish(n,[r.message],[])
    else one(list.slice(),n,[],[])})
  .catch(function(){one(list.slice(),n,[],[])})}
btn.onclick=function(){if(!SL.isEditor())return;if(on)save();else setOn(true)};
cancel.onclick=function(){if(ops.length&&!confirm('Throw away '+ops.length+' unsaved change(s)?'))return;
  ops=[];try{sessionStorage.removeItem('sl-edit')}catch(e){}location.reload()};
window.addEventListener('beforeunload',function(e){if(ops.length){e.preventDefault();e.returnValue=''}});
SL.ready.then(function(){if(!SL.isEditor()){try{sessionStorage.removeItem('sl-edit')}catch(e){}return}
  try{if(sessionStorage.getItem('sl-edit'))setOn(true)}catch(e){}});
root.addEventListener('click',function(e){if(on&&e.target.closest('a'))e.preventDefault()},true);
function hit(x,y){var el=document.elementFromPoint(x,y);return{slot:el&&el.closest('.slot'),trash:el&&el.closest('#trash')}}
function clearOver(){[].forEach.call(document.querySelectorAll('.over'),function(x){x.classList.remove('over')})}
function end(drop){if(!drag)return;var d=drag;drag=null;if(d.g)d.g.remove();d.c.classList.remove('dragging');clearOver();
  [].forEach.call(document.querySelectorAll('.ghost'),function(g){g.remove()});
  if(!drop||!d.started)return;var A=d.c.dataset.asset;
  if(d.trashed){take(A);rec({action:'remove',asset:A});return}
  if(!d.to||d.to===d.from)return;put(A,d.to.dataset.bay);rec({action:'move',asset:A,bay:d.to.dataset.bay})}
root.addEventListener('pointerdown',function(e){
  if(!on||drag||e.target.closest('button'))return;var c=e.target.closest('.mini');if(!c)return;e.preventDefault();
  drag={c:c,from:c.closest('.slot'),x0:e.clientX,y0:e.clientY,started:false}});
document.addEventListener('pointermove',function(e){
  if(!drag)return;
  if(!drag.started){if(Math.abs(e.clientX-drag.x0)+Math.abs(e.clientY-drag.y0)<8)return;
    var r=drag.c.getBoundingClientRect(),g=drag.c.cloneNode(true);g.classList.add('ghost');g.style.width=r.width+'px';
    document.body.appendChild(g);drag.g=g;drag.dx=drag.x0-r.left;drag.dy=drag.y0-r.top;drag.started=true;drag.c.classList.add('dragging')}
  e.preventDefault();drag.g.style.left=(e.clientX-drag.dx)+'px';drag.g.style.top=(e.clientY-drag.dy)+'px';
  drag.g.style.visibility='hidden';var h=hit(e.clientX,e.clientY);drag.g.style.visibility='';
  clearOver();drag.to=h.slot;drag.trashed=!!h.trash;if(h.slot)h.slot.classList.add('over');if(h.trash)trash.classList.add('over');
  if(e.clientY<70)window.scrollBy(0,-14);else if(e.clientY>window.innerHeight-70)window.scrollBy(0,14)},{passive:false});
document.addEventListener('pointerup',function(){end(true)});
document.addEventListener('pointercancel',function(){end(false)});
window.addEventListener('blur',function(){end(false)});
document.addEventListener('visibilitychange',function(){if(document.hidden)end(false)});
root.addEventListener('click',function(e){
  var b=e.target.closest('button.ed');if(!b||!on)return;e.preventDefault();e.stopPropagation();
  if(b.classList.contains('addhere')){var s=b.closest('.slot'),bay=s.dataset.bay,A=am(prompt('Asset number to add to bay '+bay+' (e.g. AM00869 or 869)'));
    if(!A)return;if(!/^AM\\d{5}$/.test(A)){msg('That is not an asset number.',1);return}
    if(card(A)){msg(A+' is already on this layout - drag it instead.',1);return}
    put(A,bay,null,true);rec({action:'place',asset:A,bay:bay});details(A)}
  else if(b.classList.contains('addbay')){var nb=b.closest('.shelf').dataset.next;addBay(nb);rec({action:'addBay',bay:nb})}
  else if(b.classList.contains('rmasset')){var A2=b.closest('.mini').dataset.asset;take(A2);rec({action:'remove',asset:A2})}
  else if(b.classList.contains('rmbay')){var s2=b.closest('.slot');if(s2.querySelector('.mini')){msg('Move or remove the assets in '+s2.dataset.bay+' first.',1);return}
    dropBay(s2.dataset.bay);rec({action:'removeBay',bay:s2.dataset.bay})}
},true);
})();
"""


def signers():
    """Names offered for 'Received by' - the DSR signer names, as EDT offers them (--refresh updates)."""
    try:
        return json.load(open(SIGNERS, encoding="utf-8"))
    except (OSError, ValueError):
        return []


def move_panel(loc, ref):
    """Send it on / Deliver it: same records as EDT through the flow."""
    if not live(loc):
        return ""
    return (f'<div class="sec"><h2 class="mono">Send it on</h2></div>'
            f'<div id="move" class="mv" data-asset="{esc(ref)}">{GATE}<div class="inner" hidden>'
            f'<div id="mvst" class="mvmsg"></div>'
            f'<div id="mvsend" hidden>{dropdown("mvto", "Going to", ["GI", "SCM", "Workshop", "Calibration Facility"], "Other place…", "Type where it is going")}'
            f'<button id="mvgo" class="btn" type="button">Send it on</button></div>'
            f'<div id="mvdel" hidden><div class="mvroute mono" id="mvroute"></div>'
            f'{dropdown("mvrecv", "Received by", signers(), "Someone else…", "Type their name", first=("", "Me (signed in)"))}'
            f'<label>Signature</label><canvas id="mvsig"></canvas>'
            f'<button id="mvclear" class="btn alt" type="button">Clear signature</button>'
            f'<button id="mvdeliver" class="btn" type="button">Deliver it</button></div></div></div>'
            f'<script>{MOVE_JS}</script>')


def bay_panel(loc, it, all_bays):
    if not live(loc):
        return ""
    return (f'<details class="minor" data-editor-only hidden><summary>Change storage bay <span class="mono">(now {esc(it["bin_location"])})</span></summary>'
            f'<div id="bay" class="mv" data-asset="{esc(it["ref"])}">{GATE}<div class="inner" hidden>'
            f'<div id="bayst" class="mvmsg"></div>'
            f'{dropdown("baynew", "Move to bay", [b for b in all_bays if b != it["bin_location"]], "New bay…", "Type the bay, e.g. 2.3.5", numeric=True)}'
            f'<button id="baymove" class="btn" type="button">Move to this bay</button>'
            f'<button id="bayremove" class="btn alt" type="button">Remove from this storage</button></div></div></details>'
            f'<script>{BAY_JS}</script>')


def shelf_panel(loc, b, s, slots):
    if not (live(loc) and loc.get("asset_register")):
        return ""
    bays = [f"{b}.{s}.{p}" for p in slots]
    nxt = f"{b}.{s}.{max(slots) + 1 if slots else 1}"
    empty = [f"{b}.{s}.{p}" for p, its in slots.items() if not its]
    return (f'<div data-editor-only hidden><div class="sec"><h2 class="mono">03 / Change this shelf</h2></div>'
            f'<div id="shelfedit" class="mv" data-next="{nxt}" data-first="{bays[0] if bays else nxt}">{GATE}'
            f'<div class="inner" hidden><div id="shst" class="mvmsg"></div>'
            f'<label>Asset number<input id="shasset" maxlength="12" autocomplete="off" placeholder="e.g. AM00869 or 869"></label>'
            f'{dropdown("shbay", "Bay", bays, "New bay…", "Type the bay, e.g. " + nxt, numeric=True)}'
            f'<button id="shadd" class="btn" type="button">Add asset to this bay</button>'
            f'<button id="shnewbay" class="btn alt" type="button">Add bay {nxt}</button>'
            f'{"".join(f"<button class=\"btn alt shrm\" type=\"button\" data-bay=\"{x}\">Remove empty bay {x}</button>" for x in empty)}'
            f'<div class="hint">The asset must already be in the Asset Register. A bay can hold several assets.</div>'
            f'</div></div></div><script>{SHELF_JS}</script>')


def transit_banner(tr):
    if not tr:
        return ""
    return (f'<div class="note transit">In transit {esc(tr.get("from") or "?")} → {esc(tr.get("to") or "?")}'
            f'{" · " + esc(tr["isr"]) if tr.get("isr") else ""} · <b>not signed for yet</b></div>')


def cal_status(reg):
    """AM-R055 CalibrationStatus (Calibrated / Out of Calibration / Indication Only / Not Required)."""
    v = (reg or {}).get("CalibrationStatus")
    return (v.get("Value") if isinstance(v, dict) else v) or ""


def cal_badge(cs):
    """Green Calibrated, red Out of Calibration; Indication Only and No Cal Required neutral (Hugo 05/11/2026)."""
    label = "No Cal Required" if cs == "Not Required" else cs
    kind = {"Calibrated": "ok", "Out of Calibration": "bad"}.get(cs, "")
    return f'<span class="badge {kind}">{esc(label)}</span>'


def asset_body(loc, it, reg, all_bays):
    b, s, p = it["bin_location"].split(".")
    rows = []

    def row(label, value):
        if value:
            rows.append(f"<dt>{esc(label)}</dt><dd>{value}</dd>")

    row("Location", f'<a href="../../{b}-{s}/#p{p}" class="mono">{esc(it["bin_location"])}</a> · {loc["unit_word"]} {b}, Shelf {s}')
    row("Asset No", f'<b class="mono">{esc(it["ref"])}</b>')
    if reg:
        row("Description", esc(reg.get("Desc2") or it["description"]))
        row("Make", esc(reg.get("Make") or it.get("make", "")))
        row("Model", esc(reg.get("Model") or it.get("model", "")))
        row("Serial No", f'<span class="mono">{esc(reg.get("SerialNo") or "")}</span>' if reg.get("SerialNo") else "")
        row("Specifications", esc(reg.get("Specification") or ""))
        row("Category", esc(" / ".join(x for x in (reg.get("Category"), reg.get("Sub_x002d_Category")) if x)))
        # AM-R055 (11/11/2026): EquipmentStatus is operational only (Active / Out of Service);
        # CalibrationStatus says whether it is in calibration
        status = reg.get("EquipmentStatus") or ""
        row("Status", f'<span class="badge {"ok" if status == "Active" else "bad"}">{esc(status)}</span>' if status else "")
        cs = cal_status(reg)
        if cs and reg.get("EquipmentRequirement") != "Calibration NATA":
            row("Calibration", cal_badge(cs))
        row("Whereabouts", esc(reg.get("Whereabouts") or ""))
        row("Requirement", esc(reg.get("EquipmentRequirement") or ""))
    else:
        row("Description", esc(it["description"]))
        row("Make / Model", esc(" ".join(x for x in (it.get("make"), it.get("model")) if x)))
    spec = "".join(rows)

    cal = []
    req = (reg or {}).get("EquipmentRequirement") or ""
    if req == "Calibration NATA":  # Hugo 05/11/2026: certs and due dates only for NATA items
        due = reg.get("DueDate")
        overdue = due and dt.datetime.fromisoformat(due.replace("Z", "+00:00")) < dt.datetime.now(dt.timezone.utc)
        crow = []
        # Hugo 05/11/2026: one Calibration Status field - "Calibrated" in green, or red when it
        # isn't - instead of dates the reader has to work out
        cs = cal_status(reg) or ("Calibrated" if due and not overdue else "Out of Calibration")
        crow.append(f'<dt>Calibration status</dt><dd>{cal_badge(cs)}</dd>')
        if reg.get("CertifyingOrganisation"):
            crow.append(f"<dt>Certified by</dt><dd>{esc(str(reg['CertifyingOrganisation']))}</dd>")
        cal.append(f'<div class="sec"><h2 class="mono">02 / {"NATA calibration" if "NATA" in req else "Calibration"}</h2></div>')
        cal.append(f'<dl class="spec">{"".join(crow)}</dl>' if crow else "")
        certs = sorted((a for a in reg.get("AttachmentFiles", []) if a["FileName"].lower().endswith(".pdf")),
                       key=lambda a: cert_date(a["FileName"]))
        if certs:
            from urllib.parse import quote
            cal.append(f'<a class="btn" target="_blank" rel="noopener" href="{SP}{quote(certs[-1]["ServerRelativeUrl"])}">Open calibration certificate</a>'
                       f'<div class="hint mono">{esc(certs[-1]["FileName"])} · opens with your Sofraco sign-in</div>')
        else:
            cal.append('<div class="note">No calibration certificate is attached to this asset in the Asset Register yet.</div>')
    folder = (reg or {}).get("Link") or {}
    docs = ""
    if folder.get("Url"):
        docs = (f'<div class="sec"><h2 class="mono">{"03" if cal else "02"} / Documents</h2></div>'
                f'<a class="btn alt" target="_blank" rel="noopener" href="{esc(folder["Url"])}">Manuals, datasheets &amp; all certificates</a>'
                f'<div class="hint">Asset folder in SharePoint · opens with your Sofraco sign-in</div>')
    missing = "" if reg else '<div class="note">This asset number was not found in the Asset Register.</div>'
    return (f'<p class="crumb mono"><a href="../../">‹ {esc(loc["title"])}</a> › <a href="../../{b}-{s}/">{loc["unit_word"]} {b} · Shelf {b}.{s}</a></p>'
            f'<div class="hero">{photo(loc, it)}<div><div class="code mono">{esc(it["bin_location"])}</div>'
            f'<div class="ref mono">{esc(it["ref"])}</div><div class="desc">{esc(it["description"])}</div></div></div>'
            f'{transit_banner(it.get("transit"))}'
            f'{move_panel(loc, it["ref"]) if reg else ""}'

            f'{missing}<div class="sec"><h2 class="mono">01 / Specifications</h2></div><dl class="spec">{spec}</dl>'
            f'{"".join(cal)}{docs}{bay_panel(loc, it, all_bays) if reg else ""}')


# -------------------------------------------------------------------- qr
def make_qr(url, path):
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=12, border=4)
    q.add_data(url)
    q.make(fit=True)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    q.make_image(fill_color="black", back_color="white").save(path)


# ----------------------------------------------------------------- build
def build():
    reg = json.load(open(REGISTRY, encoding="utf-8"))
    base = reg["base_url"]
    # Sydney time, also when GitHub's runner (UTC) builds
    try:
        from zoneinfo import ZoneInfo
        updated = dt.datetime.now(ZoneInfo("Australia/Sydney")).strftime("%d/%m/%Y %H:%M")
    except Exception:
        updated = dt.datetime.now().strftime("%d/%m/%Y %H:%M")
    os.makedirs(SITE, exist_ok=True)
    im = Image.open(LOGO)
    im.thumbnail((520, 160))
    im.save(os.path.join(SITE, "logo.png"))
    open(os.path.join(SITE, ".nojekyll"), "w").close()
    index = []
    for loc in reg["locations"]:
        loc["_base"] = base
        items = load_items(loc)
        tree, unplaced = build_tree(items)
        out = os.path.join(SITE, loc["slug"])
        keep = {"img", "asset"}
        if os.path.isdir(out):  # drop shelf pages that no longer exist
            for d in os.listdir(out):
                if d not in keep and os.path.isdir(os.path.join(out, d)):
                    shutil.rmtree(os.path.join(out, d), ignore_errors=True)
            if os.path.isdir(os.path.join(out, "asset")):
                shutil.rmtree(os.path.join(out, "asset"), ignore_errors=True)
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
            f.write(page(loc["title"], loc["title"], loc["subtitle"],
                         gated(loc, cabinet_body(loc, tree, unplaced, items) + downloads_html(loc)), 1, updated))
        qr = {"cabinet": url_for(base, loc), "shelves": {}}
        qdir = ldir(loc, "qr")
        if os.path.isdir(qdir):
            shutil.rmtree(qdir, ignore_errors=True)
        make_qr(qr["cabinet"], os.path.join(qdir, "cabinet.png"))
        for b, shelves in tree.items():
            for s, slots in shelves.items():
                d = os.path.join(out, shelf_slug(b, s))
                os.makedirs(d, exist_ok=True)
                h1 = f'{loc["title"]} · Shelf {b}.{s}'
                with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
                    f.write(page(h1, f"Shelf {b}.{s}", loc["title"],
                                 gated(loc, shelf_body(loc, b, s, slots)),
                                 2, updated).replace('src="img/', 'src="../img/'))
                qr["shelves"][shelf_key(b, s)] = url_for(base, loc, b, s)
                make_qr(qr["shelves"][shelf_key(b, s)], os.path.join(qdir, f"shelf-{shelf_slug(b, s)}.png"))
        if loc.get("asset_register"):
            all_bays = sorted({i["bin_location"] for i in items if i.get("bin_location")}, key=bay_key)
            with open(ldir(loc, "asset_links.csv"), "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)  # read by the asset-tag artwork; one row per asset
                w.writerow(["asset_no", "location", "cabinet", "shelf", "position", "description", "asset_page_url", "shelf_page_url"])
                for it in items:
                    if it.get("ref") and it.get("bin_location"):
                        c, s_, p_ = it["bin_location"].split(".")
                        w.writerow([it["ref"], it["bin_location"], c, s_, p_, it["description"],
                                    f'{url_for(base, loc)}asset/{it["ref"]}/', url_for(base, loc, c, s_)])
            for it in items:
                if it.get("ref") and it.get("bin_location"):
                    d = os.path.join(out, "asset", it["ref"])
                    os.makedirs(d, exist_ok=True)
                    with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
                        f.write(page(f'{it["ref"]} · {it["description"]}', it["ref"], loc["title"],
                                     gated(loc, asset_body(loc, it, it.get("_reg"), all_bays)),
                                     3, updated).replace('src="img/', 'src="../../img/'))
        with open(ws(loc["qr_json"]), "w", encoding="utf-8") as f:
            json.dump(qr, f, indent=2)
        n = sum(1 for i in items if i.get("description"))
        if not loc.get("hidden"):
            index.append(f'<a href="{loc["slug"]}/"><span>{esc(loc["title"])}</span><span class="mono">{n} items ›</span></a>')
        print(f'{loc["slug"]}: {n} items, {len(qr["shelves"])} shelf pages, {len(unplaced)} not placed')
    moves = [l["move"] for l in json.load(open(REGISTRY, encoding="utf-8"))["locations"] if (l.get("move") or {}).get("client_id")]
    if moves:  # sign-in return page: MSAL finishes the redirect, then sends the worker back to their asset
        mv = moves[0]
        os.makedirs(os.path.join(SITE, "auth"), exist_ok=True)
        with open(os.path.join(SITE, "auth", "index.html"), "w", encoding="utf-8") as f:
            f.write(page("Signing in", "Signing in", "Sofraco", '<p class="mvmsg">Signing you in…</p>'
                         f'<script src="{MSAL_CDN}"></script><script>'
                         f'var a=new msal.PublicClientApplication({{auth:{{clientId:{json.dumps(mv["client_id"])},'
                         f'authority:"https://login.microsoftonline.com/{mv["tenant_id"]}",redirectUri:{json.dumps(base + "auth/")},'
                         f'navigateToLoginRequestUrl:true}},cache:{{cacheLocation:"localStorage"}}}});'
                         f'a.initialize().then(function(){{return a.handleRedirectPromise()}}).then(function(r){{'
                         f'if(!r)location.replace({json.dumps(base)})}});</script>', 1, updated)
                    .replace('href="../logo.png"', 'href="../logo.png"'))
    with open(os.path.join(SITE, "index.html"), "w", encoding="utf-8") as f:
        f.write(page("Storage layouts", "Storage Layouts", "Scan the QR on a cabinet or shelf",
                     '<div class="idx">' + "".join(index) + "</div>", 0, updated).replace('href="../logo.png"', 'href="logo.png"'))


# ------------------------------------------------------------ printable PDFs
PDF_FILES = [("A3 layout", "lab_equipment_storage_a3.pdf"),
             ("Shelf stickers (Avery L7073)", "lab_equipment_storage_shelf_stickers_L7073.pdf"),
             ("Asset tags (Avery L7073)", "lab_equipment_storage_asset_tags_L7073.pdf"),
             ("Asset tags (A3, cut by hand)", "lab_equipment_storage_asset_tags_A3.pdf")]


def write_pdf_items(loc, items):
    """items.csv for the PDF generator, from the register snapshot."""
    with open(ldir(loc, "items.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["bin_location", "register_no", "description", "make", "model", "image"])
        for it in sorted((i for i in items if i.get("bin_location")), key=lambda i: (bay_key(i["bin_location"]), i["ref"])):
            w.writerow([it["bin_location"], it["ref"], it["description"], it.get("make", ""), it.get("model", ""),
                        it.get("image", "")])


def make_pdfs():
    """A3 layout, shelf stickers and asset tags for every location with "pdfs": true -> downloads/."""
    for loc in json.load(open(REGISTRY, encoding="utf-8"))["locations"]:
        if loc.get("pdfs"):
            write_pdf_items(loc, load_items(loc))
            subprocess.run([sys.executable, os.path.join(TOOLS, loc["pdfs"])], check=True)


def downloads_html(loc):
    if not loc.get("pdfs"):
        return ""
    links = "".join(f'<a class="btn alt" target="_blank" rel="noopener" href="../downloads/{f}">{esc(n)}</a>'
                    for n, f in PDF_FILES if os.path.exists(os.path.join(SITE, "downloads", f)))
    return (f'<div class="sec"><h2 class="mono">Print</h2><span class="mono">always the current layout</span></div>{links}'
            if links else "")


# ------------------------------------------------------------ FSL source
def refresh_fsl():
    """Pull the live FSL list through the signed-in browser (SharePoint REST on
    the browser session - no Graph) into the fastener-shelves items.csv.
    Bin/Shelf (BinShelf) holds the B.S.P position."""
    sys.path.insert(0, os.path.join(WORKSPACE, "scripts"))
    from playwright.sync_api import sync_playwright
    from sofraco_browser import connect, ensure_browser, page_for
    with sync_playwright() as p:
        port, _ = ensure_browser()
        _, ctx = connect(p, port)
        pg = page_for(ctx, FSL_SITE, port=port)
        try:
            if "sharepoint.com" not in pg.url:
                pg.goto(FSL_SITE)
            r = ctx.request.get(f"{FSL_SITE}/_api/web/lists(guid'{FSL_LIST}')/items?$top=5000",
                                headers={"Accept": "application/json;odata=nometadata"})
            rows = r.json()["value"]
        finally:
            pg.close()
    loc = next(l for l in json.load(open(REGISTRY))["locations"] if l.get("source") == "fsl")
    cols = ["bin_location", "item_code", "description", "make", "model", "image", "category",
            "thread_size", "length_mm", "qty", "uom", "reorder_point", "stock_status"]
    out = []
    for x in sorted(rows, key=lambda x: x.get("ItemCode") or ""):
        if x.get("Active") is False:
            continue
        fmt = lambda v: "" if v is None else (str(int(v)) if isinstance(v, float) and v.is_integer() else str(v))
        out.append({"bin_location": (x.get("BinShelf") or "").strip(), "item_code": x.get("ItemCode") or "",
                    "description": x.get("Title") or "", "make": "", "model": "", "image": x.get("ItemCode") or "",
                    "category": x.get("ComponentCategory") or "", "thread_size": x.get("ThreadSize") or "",
                    "length_mm": fmt(x.get("LengthMM")), "qty": fmt(x.get("QtyOnHand")), "uom": x.get("UnitOfMeasure") or "",
                    "reorder_point": fmt(x.get("ReorderPoint")), "stock_status": x.get("StockStatus") or ""})
    with open(ws(loc["csv"]), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, cols)
        w.writeheader()
        w.writerows(out)
    print(f"FSL: {len(out)} active rows -> {ws(loc['csv'])}")


ASSET_SITE = SP + "/sites/AssetManagement"
ASSET_LIST = "f8fbbec9-a633-479c-a691-637c1826ec4a"  # AM-R055 Asset Register
BAYS_LIST = "84d715f4-429f-419d-9f3d-113bf39f176d"   # SLB - Storage Layout Bays
ISR_SITE = SP + "/sites/Field-Service"
ISR_LIST = "bdd4169f-e683-4ed8-bd79-1efe49f1dd02"
ASSET_FIELDS = ["AssetNumber", "StorageBay", "Desc2", "Make", "Model", "SerialNo", "Specification", "Category",
                "Sub_x002d_Category", "EquipmentRequirement", "EquipmentStatus", "CalibrationStatus", "Whereabouts",
                "Calibration_x002f_InspectionDate", "DueDate", "CertifyingOrganisation",
                "RequirementFrequency_x002a_month", "Link", "AttachmentFiles"]  # never prices or contacts
ASSET_PHOTOS = os.path.join(os.environ.get("USERPROFILE", ""), r"SOFRACO\Assets - AM\01. Assets")


def refresh_layouts():
    """Write data/<slug>/layout.json from the Asset Register (Storage Location / Storage Bay),
    the bays list and the open asset ISR rows - the same file the flow commits after a change.
    Signed-in browser session REST, no Graph. Also copies missing register photos."""
    sys.path.insert(0, os.path.join(WORKSPACE, "scripts"))
    from playwright.sync_api import sync_playwright
    from sofraco_browser import connect, ensure_browser, page_for
    h = {"Accept": "application/json;odata=nometadata"}
    with sync_playwright() as p:
        port, _ = ensure_browser()
        _, ctx = connect(p, port)
        pg = page_for(ctx, ASSET_SITE, port=port)
        try:
            if "sharepoint.com" not in pg.url:
                pg.goto(ASSET_SITE)
            sel = ",".join(f for f in ASSET_FIELDS + ["StorageLocation"] if f != "AttachmentFiles")
            rows = ctx.request.get(f"{ASSET_SITE}/_api/web/lists(guid'{ASSET_LIST}')/items?$top=5000"
                                   f"&$select={sel},AttachmentFiles&$expand=AttachmentFiles"
                                   "&$filter=StorageLocation ne null", headers=h).json()["value"]
            bays = ctx.request.get(f"{ASSET_SITE}/_api/web/lists(guid'{BAYS_LIST}')/items?$top=5000&$select=Title,Bay",
                                   headers=h).json()["value"]
            isr = ctx.request.get(f"{ISR_SITE}/_api/web/lists(guid'{ISR_LIST}')/items?$top=5000"
                                  "&$select=AssetNumber,Status,From,To,WorkshopOrderNumber,Created,Tracking"
                                  "&$filter=Typeofrequest eq 'Asset' and Status eq 'In transit'", headers=h).json()["value"]
        finally:
            pg.close()
    transit = {}
    for r in sorted(isr, key=lambda r: r["Created"]):
        transit[(r.get("AssetNumber") or "").upper()] = {"isr": r.get("WorkshopOrderNumber"), "from": r.get("From"),
                                                          "to": r.get("To"), "sentAt": r.get("Created")}
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for loc in json.load(open(REGISTRY, encoding="utf-8"))["locations"]:
        if not loc.get("asset_register"):
            continue
        mine = [r for r in rows if (r.get("StorageLocation") or "").strip().lower() == loc["title"].lower()]
        assets = []
        for r in sorted(mine, key=lambda r: r.get("AssetNumber") or ""):
            a = {k: r.get(k) for k in ASSET_FIELDS}
            a["AttachmentFiles"] = [{"FileName": f["FileName"], "ServerRelativeUrl": f["ServerRelativeUrl"]}
                                    for f in r.get("AttachmentFiles") or []]
            a["transit"] = transit.get((a["AssetNumber"] or "").upper())
            assets.append(a)
            am = (a["AssetNumber"] or "").upper()
            src = os.path.join(ASSET_PHOTOS, am, am + "_OEM.png")
            dst = os.path.join(IMAGES, am + "_OEM.webp")
            if os.path.exists(src) and (not os.path.exists(dst) or os.path.getmtime(dst) < os.path.getmtime(src)):
                im = Image.open(src).convert("RGBA")
                im.thumbnail((1000, 1000))
                im.save(dst, "WEBP", quality=90, method=6)
        lay = {"location": loc["title"], "generatedAt": now,
               "bays": sorted({b["Bay"] for b in bays if (b.get("Title") or "").lower() == loc["title"].lower()
                               and BAY_RE.match(b.get("Bay") or "")}, key=bay_key),
               "assets": assets}
        os.makedirs(ldir(loc), exist_ok=True)
        with open(ldir(loc, "layout.json"), "w", encoding="utf-8") as f:
            json.dump(lay, f, indent=1)
        print(f'{loc["slug"]}: {len(assets)} assets, {len(lay["bays"])} bays, '
              f'{sum(1 for a in assets if a["transit"])} in transit')


def refresh_signers():
    """DSR signer names (most used first) for the 'Received by' combo box."""
    sys.path.insert(0, os.path.join(WORKSPACE, "scripts"))
    from collections import Counter
    from playwright.sync_api import sync_playwright
    from sofraco_browser import connect, ensure_browser, page_for
    site = SP + "/sites/Field-Service"
    with sync_playwright() as p:
        port, _ = ensure_browser()
        _, ctx = connect(p, port)
        pg = page_for(ctx, site, port=port)
        try:
            if "sharepoint.com" not in pg.url:
                pg.goto(site)
            rows = ctx.request.get(f"{site}/_api/web/lists/getbytitle('DSR - Delivery Signature Register')/items"
                                   "?$top=5000&$select=SignedByName",
                                   headers={"Accept": "application/json;odata=nometadata"}).json()["value"]
        finally:
            pg.close()
    names = Counter((r.get("SignedByName") or "").strip() for r in rows)
    names.pop("", None)
    json.dump([n for n, _ in names.most_common()], open(SIGNERS, "w", encoding="utf-8"), indent=1)
    print(f"signers: {len(names)} names")


# --------------------------------------------------------------- publish
SITE_ACCOUNT = "workshopsofraco"  # Sofraco's GitHub account (workshop@sofraco.com.au)


def git(*args):
    # Push as the Sofraco account whatever gh's active account is (fasima stays active for other projects).
    tok = subprocess.run(["gh", "auth", "token", "--user", SITE_ACCOUNT], capture_output=True, text=True).stdout.strip()
    env = dict(os.environ, GH_TOKEN=tok) if tok else None
    return subprocess.run(["git", "-C", SITE, *args], check=True, capture_output=True, text=True, env=env).stdout


def pull():
    """Changes made on the pages arrive as commits from the flow - take them first."""
    git("pull", "-q", "--rebase", "--autostash", "origin", "main")


def publish():
    git("add", "-A")
    if git("status", "--porcelain").strip():
        git("commit", "-q", "-m", "Update storage layouts " + dt.datetime.now().strftime("%d/%m/%Y %H:%M"))
    pull()
    git("push", "-q", "origin", "main")
    print("pushed - GitHub Pages refreshes within about a minute")
    return


def _old_publish():
    git("add", "-A")
    if not git("status", "--porcelain").strip():
        print("site unchanged - nothing to publish")
        return
    git("commit", "-q", "-m", "Update storage layouts " + dt.datetime.now().strftime("%d/%m/%Y %H:%M"))
    git("push", "-q", "origin", "main")
    print("pushed - GitHub Pages refreshes within about a minute")


def check():
    """Every page live and every QR decodes to its own page."""
    import cv2
    import requests
    reg = json.load(open(REGISTRY, encoding="utf-8"))
    det, bad, n = cv2.QRCodeDetector(), [], 0
    for loc in reg["locations"]:
        qr = json.load(open(ws(loc["qr_json"]), encoding="utf-8"))
        qdir = ldir(loc, "qr")
        pairs = [("cabinet.png", qr["cabinet"], loc["title"])] + \
            [(f"shelf-{k.replace('.', '-')}.png", u, f"Shelf {k}") for k, u in qr["shelves"].items()]
        for png, url, must in pairs:
            n += 1
            img = cv2.imread(os.path.join(qdir, png))
            text, _, _ = det.detectAndDecode(img)
            if not text:  # the classic detector misses some valid codes
                text, _, _ = cv2.QRCodeDetectorAruco().detectAndDecode(img)
            r = requests.get(url, timeout=30)
            ok = text == url and r.status_code == 200 and esc(must) in r.text
            if not ok:
                bad.append(f"{loc['slug']}/{png}: qr={text!r} http={r.status_code}")
    print(f"checked {n} QR codes / pages, {len(bad)} problems")
    for b in bad:
        print("  " + b)
    return not bad


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="snapshot FSL, Asset Register, bays and transit first")
    ap.add_argument("--no-pull", action="store_true")
    ap.add_argument("--pdfs", action="store_true", help="also rebuild the printable PDFs")
    ap.add_argument("--refresh-fsl", action="store_true")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        sys.exit(0 if check() else 1)
    if a.refresh or a.refresh_fsl:
        refresh_fsl()
    if not a.no_pull and os.path.isdir(os.path.join(SITE, ".git")) and not os.environ.get("CI"):
        pull()
    if a.refresh:
        refresh_layouts()
    if a.pdfs:
        make_pdfs()
    build()
    if a.publish:
        publish()
