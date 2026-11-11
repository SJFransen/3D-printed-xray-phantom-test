"""Self-contained controls shared by newly generated reports."""
import base64
import hashlib

SCRIPT = "document.getElementById('export-pdf').addEventListener('click',function(){window.print();});"
SCRIPT_HASH = base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()

def controls():
    return '<button id="export-pdf" type="button">Export to PDF</button><script>' + SCRIPT + '</script>'

def print_css(title, landscape=False):
    # CSS escapes differ from JSON (in particular non-ASCII and line breaks).
    label = '"' + ''.join(
        char if 32 <= ord(char) < 127 and char not in '\\"<>'
        else '\\' + format(ord(char), 'x') + ' ' for char in str(title)) + '"'
    orientation = "landscape" if landscape else "portrait"
    return """
button { font:inherit; padding:8px 12px; border:1px solid #41506a;
 background:#fff; color:#1d2530; border-radius:6px; cursor:pointer; margin-bottom:12px; }
button:hover { background:#eef2f6; }
button:focus-visible, summary:focus-visible { outline:2px solid #41506a; outline-offset:3px; }
@media screen and (max-width:600px) { .card { overflow-x:auto; } }
@page { size:A4 """ + orientation + "; margin:15mm 12mm; @top-left { content:" + label + """;
 font:9px sans-serif; } @bottom-right { content:counter(page); font:9px sans-serif; } }
@media print {
 body { background:#fff; } .wrap { max-width:none; padding:0; }
 #export-pdf, script { display:none; }
 .report-picture summary { display:none; }
 .card { break-inside:auto; } h2, h3 { break-after:avoid; }
 img, tr, .report-picture { break-inside:avoid; }
 img { max-height:230mm; object-fit:contain; }
 .scroll { overflow:visible; } td, th { white-space:normal; overflow-wrap:anywhere; }
 .report-picture details[open] .enlarged { display:none; }
}
""" + ("@media print { img { max-height:155mm; } }" if landscape else "")
