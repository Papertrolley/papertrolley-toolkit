# Copyright (C) 2026 Mungojerrie
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#!/usr/bin/env python3
"""
transfer_blender.py — Blender 4.0+. CLI-обёртка для batch-transfer.

Использует общее ядро из transfer_core (тот же код, что в Photo Mapper).
Файлы: <layout_dir>/transfers/<photo_basename>.png

Запуск:
    blender --background model.blend --python transfer_blender.py -- \\
        --layout layout.quads.json \\
        --photo DSC_2505.jpg --photo-meta DSC_2505.vertices.json \\
        [--photo ... --photo-meta ...] \\
        [--out-dir <папка>] [--addon-path <путь>] [--verbose]

--addon-path: директория, где лежит transfer_core.py
              (по умолчанию — папка этого скрипта, затем стандартные
              пути addons/PT_photo_mapper/).
"""

import os
import sys
import json
import argparse

import bpy

def _find_transfer_core(extra_path=None):
    """Импортировать transfer_core, найдя его в известных местах."""
    candidates = []
    if extra_path:
        candidates.append(extra_path)
    # 1. Рядом с этим скриптом
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(here)
    # 2. Стандартные пути Blender addons
    try:
        script_paths = bpy.utils.script_paths(subdir="addons") or []
    except Exception:
        script_paths = []
    for base in script_paths:
        for sub in ("PT_photo_mapper", "pt_photo_mapper"):
            candidates.append(os.path.join(base, sub))

    for path in candidates:
        if path and os.path.isfile(
                os.path.join(path, "transfer_core.py")):
            if path not in sys.path:
                sys.path.insert(0, path)
            try:
                import transfer_core
                print(f"[transfer] transfer_core из: {transfer_core.__file__}")
                return transfer_core
            except Exception as e:
                print(f"[transfer] {path}: import failed: {e}")
                continue

    raise ImportError(
        "transfer_core.py не найден. Положите его рядом со скриптом "
        "или укажите --addon-path.")


def _resolve_meta_path(photo_path):
    """Путь к *.vertices.json для фото (замена расширения)."""
    base, _ext = os.path.splitext(photo_path)
    return base + ".vertices.json"


def _iter_pairs(args):
    """Единый итератор (photo_path, meta_path) из аргументов.
    Если --photo-meta не задан — meta выводится из photo_path."""
    photos = list(args.photo or [])
    metas = list(args.photo_meta or [])

    if metas and len(metas) != len(photos):
        raise SystemExit(
            f"Ошибка: --photo ({len(photos)}) и --photo-meta "
            f"({len(metas)}) не совпадают по количеству")

    for i, photo in enumerate(photos):
        photo_abs = bpy.path.abspath(photo)
        if metas:
            meta_abs = bpy.path.abspath(metas[i])
        else:
            meta_abs = bpy.path.abspath(_resolve_meta_path(photo_abs))
        yield i, photo_abs, meta_abs


def main():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []

    ap = argparse.ArgumentParser()
    ap.add_argument("--layout", required=True)
    ap.add_argument("--photo", action="append", required=True)
    ap.add_argument("--photo-meta", action="append")
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--addon-path", default="",
                    help="Папка с transfer_core.py")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    try:
        transfer_core = _find_transfer_core(args.addon_path or None)
    except ImportError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    layout_path = bpy.path.abspath(args.layout)
    if not os.path.exists(layout_path):
        print(f"layout.quads.json не найден: {layout_path}", file=sys.stderr)
        sys.exit(1)

    with open(layout_path, "r", encoding="utf-8") as f:
        layout = json.load(f)
    layout_dir = os.path.dirname(layout_path)
    out_dir = (bpy.path.abspath(args.out_dir) if args.out_dir
               else os.path.join(layout_dir, "transfers"))
    os.makedirs(out_dir, exist_ok=True)

    print(f"Layout: {layout_path}")
    print(f"Sheet:  {layout.get('sheet_id')}")
    print(f"Islands:{len(layout.get('islands', []))}")
    print(f"Output: {out_dir}\n")

    scene = bpy.context.scene
    bpy.context.view_layer.update()

    total = len(args.photo or [])
    succeeded = 0
    failed = []

    for i, photo_path, meta_path in _iter_pairs(args):
        label = f"Фото {i+1}/{total}: {os.path.basename(photo_path)}"
        if not os.path.exists(photo_path):
            print(f"{label} — НЕ НАЙДЕНО фото: {photo_path}")
            failed.append(photo_path)
            continue
        if not os.path.exists(meta_path):
            print(f"{label} — НЕ НАЙДЕНО meta: {meta_path}")
            failed.append(photo_path)
            continue

        print(label)
        try:
            out_path, stats = transfer_core.process_photo_from_meta(
                scene, layout, photo_path, meta_path, out_dir,
                verbose=args.verbose)
            summary = ", ".join(f"{k}={v}" for k, v in stats.items() if v)
            print(f"  → {os.path.basename(out_path)} ({summary})")
            succeeded += 1
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"  ОШИБКА: {e}")
            failed.append(photo_path)

    print(f"\nГотово: {succeeded}/{total} → {out_dir}")
    if failed:
        print(f"Не удалось: {len(failed)}")
        for p in failed:
            print(f"  - {p}")
        sys.exit(1)


if __name__ == "__main__":
    main()
