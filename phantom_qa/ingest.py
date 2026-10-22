"""Loading and normalization of phantom scans (DICOM preferred, plain images fallback).

Analysis convention after normalization: **higher pixel value = more attenuation**
(the phantom appears bright, direct exposure dark). All statistics downstream are
computed on ``ScanData.pixels`` in float64.
"""

from __future__ import annotations

import hashlib
import io
import os
import zipfile
from dataclasses import dataclass, field

import numpy as np

DICOM_EXTENSIONS = {".dcm", ".dicom", ""}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}


@dataclass
class ScanData:
    pixels: np.ndarray            # float64, higher = more attenuation
    meta: dict                    # extracted metadata (JSON-serializable)
    sha256: str
    source_name: str
    kind: str                     # 'dicom' | 'image'
    reduced_precision: bool       # True for plain-image fallback
    spacing_candidates: dict = field(default_factory=dict)  # name -> mm/px
    #: The exact bytes this scan was decoded from. For a zip (CD export) that
    #: is the extracted MEMBER, which is what sha256 fingerprints — the caller
    #: that persists an upload must store these, not the container, or every
    #: later integrity check compares the container's hash against the
    #: member's and fails. Never serialised; in-memory hand-off only.
    source_bytes: bytes | None = None

    @property
    def shape(self):
        return self.pixels.shape


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _extract_dicom_meta(ds) -> dict:
    tags = [
        "SOPInstanceUID", "SOPClassUID", "Modality", "Manufacturer",
        "ManufacturerModelName", "StationName", "DetectorID", "DetectorType",
        "StudyDate", "StudyTime", "SeriesDate", "SeriesTime",
        "AcquisitionDate", "AcquisitionTime", "ContentDate", "ContentTime",
        "InstanceCreationDate", "DeviceSerialNumber",
        "StudyDescription", "SeriesDescription", "ProtocolName",
        "BodyPartExamined", "ViewPosition", "PatientOrientation",
        "KVP", "ExposureTime", "XRayTubeCurrent", "Exposure", "ExposureInuAs",
        "RelativeXRayExposure", "SensitivityValue",
        "DistanceSourceToDetector", "DistanceSourceToPatient",
        "EstimatedRadiographicMagnificationFactor",
        "ImagerPixelSpacing", "PixelSpacing", "SpatialResolution",
        "Rows", "Columns", "BitsAllocated", "BitsStored", "HighBit",
        "PixelRepresentation", "PhotometricInterpretation",
        "RescaleSlope", "RescaleIntercept", "RescaleType",
        "WindowCenter", "WindowWidth", "PresentationLUTShape",
        "AcquisitionDeviceProcessingDescription", "ImageType",
    ]
    meta = {}
    for t in tags:
        if hasattr(ds, t):
            v = getattr(ds, t)
            try:
                if isinstance(v, (list, tuple)) or v.__class__.__name__ == "MultiValue":
                    meta[t] = [str(x) for x in v]
                elif isinstance(v, (int, float)):
                    meta[t] = v
                else:
                    meta[t] = str(v)
            except Exception:
                meta[t] = str(v)
    return meta


def protocol_signature(meta: dict) -> str:
    """Compact identifier of acquisition conditions used to group comparable scans."""
    model = meta.get("ManufacturerModelName", "unknown")
    kvp = meta.get("KVP", "?")
    spacing = meta.get("ImagerPixelSpacing") or meta.get("PixelSpacing") or ["?"]
    proc = str(meta.get("AcquisitionDeviceProcessingDescription", ""))
    # Keep the processing family (e.g. "UNIQUE: S:200 L:4.0"), drop per-exposure params.
    proc_family = " ".join(proc.split()[:3]) if proc else "no-processing-info"
    signature = f"{model} | {kvp} kV | {spacing[0]} mm/px | {proc_family}"
    # Keep the legacy signature when identity is unavailable. Identified scans
    # use a new namespace; never silently match them to an ambiguous old reference.
    identity = {key: str(meta[key]).strip() for key in
                ("DeviceSerialNumber", "DetectorID", "StationName")
                if str(meta.get(key) or "").strip()}
    if identity:
        import json
        machine = _sha256(json.dumps(identity, sort_keys=True).encode())[:20]
        signature += f" | machine-v2:{machine}"
    return signature


def load_dicom_bytes(data: bytes, source_name: str) -> ScanData:
    import pydicom

    ds = pydicom.dcmread(io.BytesIO(data))
    arr = ds.pixel_array.astype(np.float64)

    slope = float(getattr(ds, "RescaleSlope", 1) or 1)
    intercept = float(getattr(ds, "RescaleIntercept", 0) or 0)
    arr = arr * slope + intercept

    photometric = str(getattr(ds, "PhotometricInterpretation", "MONOCHROME2"))
    if photometric == "MONOCHROME1":
        arr = float(arr.max()) - arr

    meta = _extract_dicom_meta(ds)
    meta["TransferSyntax"] = ds.file_meta.TransferSyntaxUID.name
    meta["_polarity"] = "attenuation-high"

    spacing = {}
    if hasattr(ds, "ImagerPixelSpacing"):
        spacing["ImagerPixelSpacing"] = float(ds.ImagerPixelSpacing[0])
    if hasattr(ds, "PixelSpacing"):
        spacing["PixelSpacing"] = float(ds.PixelSpacing[0])

    return ScanData(
        pixels=arr,
        meta=meta,
        sha256=_sha256(data),
        source_name=source_name,
        kind="dicom",
        reduced_precision=False,
        spacing_candidates=spacing,
        source_bytes=data,
    )


def load_image_bytes(data: bytes, source_name: str) -> ScanData:
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    arr = np.asarray(img.convert("L") if img.mode not in ("I;16", "I") else img,
                     dtype=np.float64)
    meta = {
        "Rows": arr.shape[0], "Columns": arr.shape[1],
        "SourceFormat": img.format, "_polarity": "assumed-attenuation-high",
    }
    return ScanData(
        pixels=arr,
        meta=meta,
        sha256=_sha256(data),
        source_name=source_name,
        kind="image",
        reduced_precision=True,
        spacing_candidates={},
        source_bytes=data,
    )


def _is_dicom_bytes(data: bytes) -> bool:
    return len(data) > 132 and data[128:132] == b"DICM"


def load_any_bytes(data: bytes, source_name: str) -> list[ScanData]:
    """Load a file of any supported kind. Zip archives (CD exports) are walked for
    DICOM images; DICOMDIR indexes are skipped in favor of scanning actual files."""
    name_lower = source_name.lower()
    if name_lower.endswith(".zip"):
        # Decompression bombs: infolist() reports the DECLARED uncompressed
        # size, so both caps are enforced before a byte is inflated. A real
        # detector image is tens of MB; the caps are far above any legitimate
        # CD export and far below what would take a gunicorn worker down.
        MAX_MEMBER = 512 * 1024 * 1024
        MAX_TOTAL = 1024 * 1024 * 1024
        total = 0
        scans = []
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir() or info.file_size < 5000:
                    continue
                if info.file_size > MAX_MEMBER:
                    continue
                base = os.path.basename(info.filename).lower()
                if base in ("dicomdir",) or base.endswith((".jar", ".exe", ".pdf",
                                                           ".htm", ".html", ".xml",
                                                           ".css", ".png", ".txt",
                                                           ".inf", ".cmd", ".sh")):
                    continue
                # budget only what is actually inflated
                if total + info.file_size > MAX_TOTAL:
                    break
                total += info.file_size
                content = zf.read(info)
                if _is_dicom_bytes(content):
                    try:
                        scans.append(load_dicom_bytes(content, info.filename))
                    except Exception:
                        continue
        if not scans:
            raise ValueError("No readable DICOM images found in the zip archive")
        return scans

    if _is_dicom_bytes(data):
        return [load_dicom_bytes(data, source_name)]

    ext = os.path.splitext(name_lower)[1]
    if ext in IMAGE_EXTENSIONS:
        return [load_image_bytes(data, source_name)]

    # Last resort: try DICOM without preamble, then image.
    try:
        return [load_dicom_bytes(data, source_name)]
    except Exception:
        return [load_image_bytes(data, source_name)]


def load_path(path: str) -> list[ScanData]:
    with open(path, "rb") as f:
        data = f.read()
    return load_any_bytes(data, os.path.basename(path))
