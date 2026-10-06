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

def camera_params(path):
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
