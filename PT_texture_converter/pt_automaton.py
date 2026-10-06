#!/usr/bin/env python3
"""
PBA Texture Transfer — перенос текстур с фото на выкройку через гомографии.

Использование:
    python transfer.py --layout layout.png --quads layout.quads.json \\
                       --photo DSC_6166.jpg --csv DSC_6166.vertices.csv \\
                       --out result.png [--bg transparent|layout] \\
                       [--photo ... --csv ...] ...

Опции:
    --layout    PNG-выкройка (A4, 600 dpi)
    --quads     JSON с квадами (из PBA Layout Mapper)
    --photo     фотография (можно повторять)
    --csv       CSV с позициями вершин (можно повторять, по одному на --photo)
    --out       выходной PNG
    --bg        transparent (по умолчанию) или layout
    --verbose   печатать прогресс по каждому кваду
"""
import argparse
import json
import os
import sys

import numpy as np
from PIL import Image


def compute_homography(src_pts, dst_pts):
    A = []
    for (x, y), (u, v) in zip(src_pts, dst_pts):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y, -u])
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y, -v])
    A = np.array(A, dtype=np.float64)
    _, _, Vt = np.linalg.svd(A)
    H = Vt[-1].reshape(3, 3)
    n = np.linalg.norm(H)
    if n < 1e-12:
        return None
    return H / n


def load_vertex_csv(path):
    out = {}
    with open(path, 'r', encoding='utf-8') as f:
        header = f.readline().strip().split(',')
        try:
            i_obj = header.index('object')
            i_v = header.index('vertex')
            i_x = header.index('x_px')
            i_y = header.index('y_px')
            i_if = header.index('in_frame')
            i_vis = header.index('visible')
        except ValueError:
            return out
        for line in f:
            parts = line.rstrip('\n').split(',')
            if len(parts) < 7:
                continue
            try:
                out[(parts[i_obj], int(parts[i_v]))] = (
                    float(parts[i_x]),
                    float(parts[i_y]),
                    int(parts[i_if]) == 1 and int(parts[i_vis]) == 1,
                )
            except Exception:
                continue
    return out


def bilinear(img, x, y):
    H, W, _ = img.shape
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    x1 = x0 + 1
    y1 = y0 + 1
    fx = (x - x0).astype(np.float32)[:, None]
    fy = (y - y0).astype(np.float32)[:, None]
    x0c = np.clip(x0, 0, W - 1)
    x1c = np.clip(x1, 0, W - 1)
    y0c = np.clip(y0, 0, H - 1)
    y1c = np.clip(y1, 0, H - 1)
    p00 = img[y0c, x0c]
    p10 = img[y0c, x1c]
    p01 = img[y1c, x0c]
    p11 = img[y1c, x1c]
    top = p00 * (1 - fx) + p10 * fx
    bot = p01 * (1 - fx) + p11 * fx
    return top * (1 - fy) + bot * fy


def point_in_quad(px, py, corners):
    signs = []
    for i in range(4):
        x1, y1 = corners[i]
        x2, y2 = corners[(i + 1) % 4]
        signs.append((x2 - x1) * (py - y1) - (y2 - y1) * (px - x1))
    signs = np.stack(signs, axis=0)
    return np.all(signs >= 0, axis=0) | np.all(signs <= 0, axis=0)


def main():
    ap = argparse.ArgumentParser(
        description="Перенос текстур с фото на выкройку")
    ap.add_argument("--layout", required=True)
    ap.add_argument("--quads", required=True)
    ap.add_argument("--photo", action="append", required=True)
    ap.add_argument("--csv", action="append", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--bg", choices=["transparent", "layout"],
                    default="transparent")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if len(args.photo) != len(args.csv):
        print("Ошибка: число --photo и --csv не совпадает", file=sys.stderr)
        sys.exit(1)

    for path in [args.layout, args.quads] + args.photo + args.csv:
        if not os.path.exists(path):
            print(f"Файл не найден: {path}", file=sys.stderr)
            sys.exit(1)

    log = print if args.verbose else (lambda *a, **k: None)

    layout = Image.open(args.layout).convert("RGBA")
    W_out, H_out = layout.size
    print(f"Выкройка: {W_out}×{H_out} px")

    with open(args.quads, 'r', encoding='utf-8') as f:
        quads_data = json.load(f)
    quads = quads_data.get("quads", [])
    print(f"Квадов: {len(quads)}")

    loaded = []
    for photo_path, csv_path in zip(args.photo, args.csv):
        try:
            img = np.array(Image.open(photo_path).convert("RGB"),
                           dtype=np.float32)
        except Exception as e:
            print(f"Ошибка чтения {photo_path}: {e}", file=sys.stderr)
            sys.exit(1)
        verts = load_vertex_csv(csv_path)
        loaded.append({"img": img, "verts": verts, "path": photo_path})
        print(f"  {os.path.basename(photo_path)}: "
              f"{img.shape[1]}×{img.shape[0]}, вершин {len(verts)}")

    if args.bg == "layout":
        output = np.array(layout, dtype=np.uint8)
    else:
        output = np.zeros((H_out, W_out, 4), dtype=np.uint8)

    ok = no_photo = degenerate = 0

    for qi, q in enumerate(quads):
        qid = q.get("id", f"q_{qi}")
        c3d = q.get("corners_3d", [])
        cpx = q.get("corners_layout_px", [])
        if len(c3d) != 4 or len(cpx) != 4:
            degenerate += 1
            continue

        keys = [(c["obj"], c["vertex"]) for c in c3d]

        src = None
        src_pts = None
        for ph in loaded:
            pts = []
            good = True
            for k in keys:
                v = ph["verts"].get(k)
                if v is None or not v[2]:
                    good = False
                    break
                pts.append((v[0], v[1]))
            if good:
                src = ph
                src_pts = np.array(pts, dtype=np.float64)
                break

        if src is None:
            no_photo += 1
            log(f"  {qid}: нет фото со всеми видимыми углами")
            continue

        dst_pts = np.array(cpx, dtype=np.float64)
        H_p2l = compute_homography(src_pts, dst_pts)
        if H_p2l is None:
            degenerate += 1
            continue
        try:
            H_l2p = np.linalg.inv(H_p2l)
        except np.linalg.LinAlgError:
            degenerate += 1
            continue

        xs, ys = dst_pts[:, 0], dst_pts[:, 1]
        x0 = max(0, int(np.floor(xs.min())))
        x1 = min(W_out - 1, int(np.ceil(xs.max())))
        y0 = max(0, int(np.floor(ys.min())))
        y1 = min(H_out - 1, int(np.ceil(ys.max())))
        if x1 < x0 or y1 < y0:
            degenerate += 1
            continue

        yy, xx = np.mgrid[y0:y1 + 1, x0:x1 + 1]
        px = xx.astype(np.float64) + 0.5
        py = yy.astype(np.float64) + 0.5
        inside = point_in_quad(px, py, dst_pts)
        if not inside.any():
            degenerate += 1
            continue

        fx_ = px[inside]
        fy_ = py[inside]
        ones = np.ones(fx_.size)
        sh = H_l2p @ np.stack([fx_, fy_, ones], axis=0)
        w = sh[2, :]
        safe = np.where(np.abs(w) > 1e-12, w, 1.0)
        sx = sh[0, :] / safe
        sy = sh[1, :] / safe

        Hp, Wp, _ = src["img"].shape
        inb = (np.abs(w) > 1e-12) & (sx >= 0) & (sx < Wp - 1) \
              & (sy >= 0) & (sy < Hp - 1)
        if not inb.any():
            continue

        colors = bilinear(src["img"], sx[inb], sy[inb])
        iy = yy[inside][inb]
        ix = xx[inside][inb]
        output[iy, ix, 0:3] = np.clip(colors, 0, 255).astype(np.uint8)
        output[iy, ix, 3] = 255
        ok += 1
        log(f"  {qid}: ок, {colors.shape[0]} px")

    Image.fromarray(output, "RGBA").save(args.out, "PNG")
    print(f"Сохранено: {args.out}")
    print(f"Итог: ок {ok}, без фото {no_photo}, вырожденных {degenerate}")


if __name__ == "__main__":
    main()