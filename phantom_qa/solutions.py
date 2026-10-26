"""Local, text-led troubleshooting. No network or executable content."""
from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent / "solutions"
FEATURES = {"linepairs": "line-pair", "lowcontrast": "low-contrast",
            "uniformity": "uniformity", "wedge": "dynamic-range",
            "dimensions": "dimensions", "field_alignment": "field-alignment"}
MEDIA = {".png", ".jpg", ".jpeg", ".mp4", ".webm", ".ogv", ".mov"}
IMAGES = {".png", ".jpg", ".jpeg"}
TOKEN = re.compile(r"\[(image|video)(?:\s+([^\]]+))?\]", re.IGNORECASE)


def media_path(relative):
    try:
        root = ROOT.resolve()
        path = (root / relative).resolve()
        if (not path.is_relative_to(root) or path.suffix.lower() not in MEDIA
                or not path.is_file()):
            return None
    except (OSError, ValueError, RuntimeError):
        return None
    return path


def _children(path):
    try:
        return sorted(path.iterdir())
    except OSError:
        return []


def _blocks(text, directory):
    blocks, offset = [], 0
    for match in TOKEN.finditer(text):
        if match.start() > offset:
            blocks.append({"type": "text", "text": text[offset:match.start()]})
        kind, name = match.group(1).lower(), (match.group(2) or "").strip()
        allowed = IMAGES if kind == "image" else MEDIA - IMAGES
        candidates = []
        for path in _children(directory):
            if path.suffix.lower() not in allowed:
                continue
            if not name or path.name == name or path.stem in (name, f"{kind} {name}"):
                relative = path.relative_to(ROOT).as_posix()
                if media_path(relative):
                    candidates.append(relative)
        if len(candidates) == 1:
            blocks.append({"type": kind, "url": "api/solutions/" + quote(candidates[0], safe="/"),
                           "description": name or kind})
        else:
            blocks.append({"type": "text", "text": f"[{kind.capitalize()} unavailable: {name or 'specify a filename'}]"})
        offset = match.end()
    if offset < len(text):
        blocks.append({"type": "text", "text": text[offset:]})
    return blocks


def load_steps(feature):
    """Common text first, then optional audience text; missing folders are valid."""
    folder = ROOT / FEATURES.get(feature, "__missing__")
    if not folder.is_dir() or not folder.resolve().is_relative_to(ROOT.resolve()):
        return []
    steps = []
    for directory in _children(folder):
        match = re.fullmatch(r"step (\d+)", directory.name, re.IGNORECASE)
        if not match or not directory.is_dir() or not directory.resolve().is_relative_to(ROOT.resolve()):
            continue
        texts = []
        for path in _children(directory):
            if path.suffix.lower() != ".txt" or not path.is_file():
                continue
            if not path.resolve().is_relative_to(ROOT.resolve()):
                continue
            audience = path.stem if path.stem in ("operator", "technician", "physicist") else "all"
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError):
                text = "This explanation is unavailable. Contact your technical support team."
            texts.append({"audience": audience, "blocks": _blocks(text, directory)})
        if texts:
            steps.append({"number": int(match.group(1)), "title": f"Step {int(match.group(1))}", "content": texts})
    return sorted(steps, key=lambda step: step["number"])


def feature_keys(feature):
    if feature["id"] != "geometry":
        return [feature["id"]] if feature["id"] in FEATURES else []
    paths = " ".join(issue.get("path", "") for issue in feature.get("issues", []))
    keys = []
    if "field" in paths:
        keys.append("field_alignment")
    if not keys or any(part in paths for part in ("dimension", "ruler", "scale", "corner")):
        keys.append("dimensions")
    return keys
