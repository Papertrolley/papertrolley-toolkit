from PIL import Image
from PIL.ExifTags import TAGS


KNOWN_SENSORS = {
    "NIKON Z 50": (23.5, 15.7),
    "NIKON Z 5":  (35.9, 23.9),
    "NIKON Z 6":  (35.9, 23.9),
    "NIKON Z 7":  (35.9, 23.9),
    "NIKON Z 8":  (35.9, 23.9),
    "NIKON Z 9":  (35.9, 23.9),
    "NIKON Z fc": (23.5, 15.7),
    "NIKON Z 30": (23.5, 15.7),
}

def sensor_from_model(model_str):
    if not model_str:
        return None
    key = model_str.strip().upper()
    for name, (w, h) in KNOWN_SENSORS.items():
        if name.upper() in key:
            return w, h
    return None


def _to_float(v):
    if v is None:
        return None
    try:
        if isinstance(v, (tuple, list)) and len(v) == 2:
            num, den = v
            if den == 0:
                return None
            return float(num) / float(den)
        return float(v)
    except Exception:
        return None


def read_exif(path):
    result = {}
    try:
        img = Image.open(path)
    except Exception:
        return result
    try:
        exif = img.getexif()
    except Exception:
        return result
    if not exif:
        return result

    for tag_id, value in exif.items():
        result[TAGS.get(tag_id, tag_id)] = value
    try:
        ifd = exif.get_ifd(0x8769)
        for tag_id, value in ifd.items():
            result[TAGS.get(tag_id, tag_id)] = value
    except Exception:
        pass
    return result


def camera_params(path):
    """Возвращает (focal_mm | None, sensor_mm | None)."""
    exif = read_exif(path)
    focal = _to_float(exif.get("FocalLength"))
    focal35 = _to_float(exif.get("FocalLengthIn35mmFilm"))

    sensor = None
    known = sensor_from_model(exif.get("Model"))
    if known is not None:
        sensor = known[0]
    elif focal and focal35 and focal35 > 0:
        sensor = 36.0 * focal / focal35

    return focal, sensor


def to_float(v):
    """Публичная обёртка для конверсии EXIF-значений."""
    return _to_float(v)

KNOWN_NATIVE_RESOLUTIONS = {
    "NIKON Z 50": (5568, 3712),
    "NIKON Z 5":  (6016, 4016),
    "NIKON Z 6":  (6048, 4024),
    "NIKON Z 7":  (8256, 5504),
    "NIKON Z 8":  (8256, 5504),
    "NIKON Z 9":  (8256, 5504),
    "NIKON Z fc": (5568, 3712),
    "NIKON Z 30": (5568, 3712),
    "NIKON D750": (6016, 4016),
    "NIKON D850": (8256, 5504),
    "CANON EOS 5D MARK IV": (6720, 4480),
    "CANON EOS R5": (8192, 5464),
    "CANON EOS R6": (5472, 3648),
    "SONY ILCE-7M3": (6000, 4000),
    "SONY ILCE-7M4": (7008, 4672),
    "SONY ILCE-7RM4": (9504, 6336),
}


def native_resolution_from_model(model_str):
    if not model_str:
        return None
    key = model_str.strip().upper()
    for name, res in KNOWN_NATIVE_RESOLUTIONS.items():
        if name.upper() in key:
            return res
    return None


def native_resolution_from_exif(exif):
    """Пытаемся достать нативные размеры из тегов EXIF."""
    w = exif.get("ExifImageWidth") or exif.get("PixelXDimension")
    h = exif.get("ExifImageHeight") or exif.get("PixelYDimension")
    try:
        w = int(w) if w else None
        h = int(h) if h else None
    except Exception:
        return None
    if w and h and w > 0 and h > 0:
        return (w, h)
    return None


def detect_native_resolution(path, W_actual, H_actual, verbose=False):
    """Возвращает (W_native, H_native, source).

    source ∈ {'model', 'exif', 'same'}.
    """
    exif = read_exif(path)
    model = exif.get("Model", "")

    from_model = native_resolution_from_model(model)
    if from_model is not None:
        w, h = from_model
        if W_actual <= w and H_actual <= h:
            return (w, h, "model")

    from_exif = native_resolution_from_exif(exif)
    if from_exif is not None:
        w, h = from_exif
        if W_actual <= w and H_actual <= h:
            return (w, h, "exif")

    return (W_actual, H_actual, "same")
