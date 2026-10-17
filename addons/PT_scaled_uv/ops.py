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
import json
import os

import bpy
import bmesh
import numpy as np
from PIL import Image, ImageDraw


MM_PER_INCH = 25.4

PBA_VERTEX_ID_ATTR = "pba_vertex_id"


def get_uuid_attribute(mesh, create=False):
    attr = mesh.attributes.get(PBA_VERTEX_ID_ATTR)
    if attr is None and create:
        attr = mesh.attributes.new(
            name=PBA_VERTEX_ID_ATTR, type='INT', domain='POINT')
    return attr


def get_vertex_uuid(obj, vertex_index):
    attr = get_uuid_attribute(obj.data, create=False)
    if attr is None:
        return 0
    if len(attr.data) == 0:
        return 0
    n = len(obj.data.vertices)
    if vertex_index < 0 or vertex_index >= n:
        return 0
    try:
        return int(attr.data[vertex_index].value)
    except Exception:
        return 0


def find_vertex_by_uuid(obj, uuid_val, hint_world=None):
    if uuid_val <= 0:
        return -1, 0
    attr = get_uuid_attribute(obj.data, create=False)
    if attr is None:
        return -1, 0
    if len(attr.data) == 0:
        return -1, 0

    n = min(len(obj.data.vertices), len(attr.data))
    candidates = []
    for i in range(n):
        try:
            if int(attr.data[i].value) == uuid_val:
                candidates.append(i)
        except Exception:
            continue

    if not candidates:
        return -1, 0
    if len(candidates) == 1:
        return candidates[0], 1

    if hint_world is not None:
        from mathutils import Vector
        M = obj.matrix_world
        hint = Vector(hint_world)
        best_i = candidates[0]
        best_d = float('inf')
        for i in candidates:
            p = M @ obj.data.vertices[i].co
            d = (p - hint).length
            if d < best_d:
                best_d = d
                best_i = i
        return best_i, len(candidates)

    return candidates[0], len(candidates)


def _set_status(scene, msg):
    scene.pba_uv_exporter.status_msg = msg
    print(f"[PBA UVE] {msg}")


def _get_uv_bbox_and_scale(obj, uv_layer_name):
    """Возвращает (u_min, v_min, u_max, v_max, meters_per_uv)."""
    me = obj.data
    bm = bmesh.new()
    try:
        bm.from_mesh(me)
        bm.faces.ensure_lookup_table()
        uv_layer = bm.loops.layers.uv.get(uv_layer_name)
        if uv_layer is None:
            return None

        u_min = v_min = float('inf')
        u_max = v_max = float('-inf')
        total_uv_area = 0.0
        total_3d_area = 0.0

        for f in bm.faces:
            loops = list(f.loops)
            if len(loops) < 3:
                continue
            uvs = [l[uv_layer].uv.copy() for l in loops]

            for uv in uvs:
                u_min = min(u_min, uv.x)
                u_max = max(u_max, uv.x)
                v_min = min(v_min, uv.y)
                v_max = max(v_max, uv.y)

            # UV-площадь многоугольника (shoelace)
            uv_area = 0.0
            for i in range(len(uvs)):
                x1, y1 = uvs[i].x, uvs[i].y
                x2, y2 = uvs[(i + 1) % len(uvs)].x, uvs[(i + 1) % len(uvs)].y
                uv_area += x1 * y2 - x2 * y1
            total_uv_area += abs(uv_area) * 0.5

            # 3D-площадь многоугольника
            coords = [l.vert.co.copy() for l in loops]
            if len(coords) == 3:
                total_3d_area += (coords[1] - coords[0]).cross(
                    coords[2] - coords[0]).length * 0.5
            else:
                # fan-триангуляция
                for i in range(1, len(coords) - 1):
                    total_3d_area += (coords[i] - coords[0]).cross(
                        coords[i + 1] - coords[0]).length * 0.5

        if total_uv_area < 1e-12:
            return None

        meters_per_uv = float(np.sqrt(total_3d_area / total_uv_area))
        return u_min, v_min, u_max, v_max, meters_per_uv
    finally:
        bm.free()


def _collect_edges_2d(obj, uv_layer_name, transform_fn):
    """Возвращает список 2D-рёбер в пиксельных координатах."""
    me = obj.data
    bm = bmesh.new()
    try:
        bm.from_mesh(me)
        uv_layer = bm.loops.layers.uv.get(uv_layer_name)
        if uv_layer is None:
            return []

        segments = set()
        for f in bm.faces:
            loops = list(f.loops)
            n = len(loops)
            for i in range(n):
                l1 = loops[i]
                l2 = loops[(i + 1) % n]
                uv1 = transform_fn(l1[uv_layer].uv)
                uv2 = transform_fn(l2[uv_layer].uv)
                a = (round(uv1[0], 2), round(uv1[1], 2))
                b = (round(uv2[0], 2), round(uv2[1], 2))
                segments.add((a, b) if a < b else (b, a))
        return list(segments)
    finally:
        bm.free()


def _draw_all_vertex_markers(draw, obj, uv_name, u_min, v_max, ox, oy, ppu,
                              marker_color, marker_size,
                              extra_x=0.0, extra_y=0.0):
    """Рисует маркер в каждой уникальной UV-координате каждой вершины.
    Вершины на швах дают по несколько маркеров.
    Возвращает количество нарисованных маркеров."""
    me = obj.data
    bm = bmesh.new()
    try:
        bm.from_mesh(me)
        uv_layer = bm.loops.layers.uv.get(uv_name)
        if uv_layer is None:
            return 0

        seen = set()
        drawn = 0
        ms = marker_size
        for face in bm.faces:
            for loop in face.loops:
                uv = loop[uv_layer].uv
                key = (loop.vert.index, round(uv.x, 6), round(uv.y, 6))
                if key in seen:
                    continue
                seen.add(key)

                px_x = ox + (uv.x - u_min) * ppu + extra_x
                px_y = oy + (v_max - uv.y) * ppu + extra_y
                if ms > 1 :
                    draw.ellipse(
                       [px_x - ms, px_y - ms, px_x + ms, px_y + ms],
                       fill=marker_color)
                else :
                    draw.point(
                       [px_x, px_y],
                       fill=marker_color)
                drawn += 1
        return drawn
    finally:
        bm.free()


class PBA_UVE_OT_export(bpy.types.Operator):
    bl_idname = "pba_uv_export.export"
    bl_label = "Export UV Layout"
    bl_description = ("Экспортировать UV-развёртку всех выделенных "
                      "объектов в один PNG с калибровкой")
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_uv_exporter

        objs = [o for o in context.selected_objects
                if o.type == 'MESH' and o.data.uv_layers]
        if not objs:
            if (context.active_object is not None
                    and context.active_object.type == 'MESH'
                    and context.active_object.data.uv_layers):
                objs = [context.active_object]
            else:
                self.report(
                    {'ERROR'},
                    "Выделите один или несколько mesh-объектов с UV")
                return {'CANCELLED'}

        scale_factor = pba.scale_den / pba.scale_num
        px_per_meter = 1000.0 / scale_factor * pba.dpi / MM_PER_INCH
        margin_px = pba.margin_mm * pba.dpi / MM_PER_INCH
        gap_px = pba.gap_mm * pba.dpi / MM_PER_INCH

        print("[PBA UVE] Разбор объектов:")
        layout_items = []
        skipped = []
        for obj in objs:
            uv_name = "NONE"
            if obj.data.uv_layers.active is not None:
                uv_name = obj.data.uv_layers.active.name
            else:
                uv_name = obj.data.uv_layers[0].name

            bbox = _get_uv_bbox_and_scale(obj, uv_name)
            if bbox is None:
                skipped.append((obj.name, "нет UV-геометрии"))
                print(f"  {obj.name}: ПРОПУСК (нет UV-геометрии)")
                continue
            u_min, v_min, u_max, v_max, meters_per_uv = bbox

            px_per_uv = meters_per_uv * px_per_meter
            w_px = (u_max - u_min) * px_per_uv
            h_px = (v_max - v_min) * px_per_uv

            if w_px < 1 or h_px < 1:
                skipped.append((obj.name, "слишком мелкий"))
                print(f"  {obj.name}: ПРОПУСК (слишком мелкий, "
                      f"{w_px:.1f}×{h_px:.1f})")
                continue

            if w_px < pba.min_object_px and h_px < pba.min_object_px:
                skipped.append((obj.name, f"малый {w_px:.0f}×{h_px:.0f}"))
                print(f"  {obj.name}: ПРОПУСК (малый, "
                      f"{w_px:.1f}×{h_px:.1f} px)")
                continue

            layout_items.append({
                "obj": obj,
                "uv_name": uv_name,
                "u_min": u_min, "v_min": v_min,
                "u_max": u_max, "v_max": v_max,
                "meters_per_uv": meters_per_uv,
                "px_per_uv": px_per_uv,
                "w_px": w_px,
                "h_px": h_px,
                "x_px": 0.0, "y_px": 0.0,
            })
            print(f"  {obj.name}: {w_px:.0f}×{h_px:.0f} px")

        if not layout_items:
            self.report({'ERROR'}, "Нет объектов с валидной UV")
            return {'CANCELLED'}

        # Раскладка shelf-packing с сортировкой по высоте
        # (большие сверху — они задают высоту строки)
        wrap_w = pba.wrap_width_px if pba.wrap_width_px > 0 else 0
        if wrap_w <= 0:
            # Без обёртки — оценим разумную ширину как корень из суммы
            # площадей, умноженный на 1.5
            total_area = sum((it["w_px"] + gap_px)
                             * (it["h_px"] + gap_px)
                             for it in layout_items)
            wrap_w = int(round(max(
                max(it["w_px"] for it in layout_items) + 2 * margin_px,
                (total_area ** 0.5) * 1.5)))

        sorted_items = sorted(layout_items,
                              key=lambda it: it["h_px"], reverse=True)

        cursor_x = margin_px
        cursor_y = margin_px
        row_height = 0.0
        max_x = 0.0
        max_y = 0.0

        for item in sorted_items:
            need = item["w_px"] + gap_px
            if cursor_x > margin_px and cursor_x + need > wrap_w:
                # Новая строка
                cursor_x = margin_px
                cursor_y += row_height + gap_px
                row_height = 0.0

            item["x_px"] = cursor_x
            item["y_px"] = cursor_y

            cursor_x += need
            row_height = max(row_height, item["h_px"])
            max_x = max(max_x, item["x_px"] + item["w_px"])
            max_y = max(max_y, item["y_px"] + item["h_px"])

        canvas_w = int(round(max_x + margin_px))
        canvas_h = int(round(max_y + margin_px))

        if canvas_w > 30000 or canvas_h > 30000:
            self.report(
                {'ERROR'},
                f"Полотно слишком большое: {canvas_w}×{canvas_h}. "
                f"Проверьте scale и DPI.")
            return {'CANCELLED'}

        img = Image.new("RGBA", (canvas_w, canvas_h),
                        (255, 255, 255, 255))
        draw = ImageDraw.Draw(img)
        color = tuple(int(c * 255) for c in pba.draw_line_color) + (255,)
        lw = pba.line_width_px

        obj_meta = []
        for idx, item in enumerate(layout_items):
            obj = item["obj"]
            ox, oy = item["x_px"], item["y_px"]
            u_min, v_max = item["u_min"], item["v_max"]
            ppu = item["px_per_uv"]

            def uv_to_px(uv, ox=ox, oy=oy, u_min=u_min, v_max=v_max, ppu=ppu):
                x = ox + (uv.x - u_min) * ppu
                y = oy + (v_max - uv.y) * ppu
                return (x, y)

            segments = _collect_edges_2d(obj, item["uv_name"], uv_to_px)

            # Основное полотно
            for a, b in segments:
                draw.line([a, b], fill=color, width=lw)

            # Маркеры всех вершин
            marker_color_px = tuple(
                int(c * 255) for c in pba.marker_color) + (255,)
            n_markers = _draw_all_vertex_markers(
                draw, obj, item["uv_name"],
                u_min, v_max, ox, oy, ppu,
                marker_color_px, pba.marker_size_px)
            item["n_markers"] = n_markers

            # Подписи (debug)
            if pba.debug_labels:
                label = f"{idx}: {obj.name}"
                lx = int(ox) + 5
                ly = int(oy) + 5
                draw.text((lx, ly), label, fill=(200, 0, 0, 255))

            # Отдельный PNG для объекта
            if pba.per_object_pngs:
                w_px = int(round(item["w_px"]))
                h_px = int(round(item["h_px"]))
                if w_px > 0 and h_px > 0 and w_px < 20000 and h_px < 20000:
                    sub_img = Image.new("RGBA", (w_px + 2 * lw, h_px + 2 * lw),
                                        (255, 255, 255, 255))
                    sub_draw = ImageDraw.Draw(sub_img)
                    for a, b in segments:
                        a_shift = (a[0] - ox + lw, a[1] - oy + lw)
                        b_shift = (b[0] - ox + lw, b[1] - oy + lw)
                        sub_draw.line([a_shift, b_shift],
                                      fill=color, width=lw)
                    _draw_all_vertex_markers(
                        sub_draw, obj, item["uv_name"],
                        u_min, v_max, 0.0, 0.0, ppu,
                        marker_color_px, pba.marker_size_px,
                        extra_x=lw, extra_y=lw)
                    # Сохраним после — накопим список
                    item["_sub_img"] = sub_img
                      

            obj_meta.append({
                "name": obj.name,
                "uv_layer": item["uv_name"],
                "uv_bbox": [item["u_min"], item["v_min"],
                            item["u_max"], item["v_max"]],
                "canvas_bbox_px": [
                    int(round(item["x_px"])),
                    int(round(item["y_px"])),
                    int(round(item["x_px"] + item["w_px"])),
                    int(round(item["y_px"] + item["h_px"])),
                ],
                "meters_per_uv": item["meters_per_uv"],
                "px_per_uv": item["px_per_uv"],
                "n_markers": item.get("n_markers", 0),
            })

        out_path = bpy.path.abspath(pba.output_path)
        if not out_path:
            self.report({'ERROR'}, "Не задан путь сохранения")
            return {'CANCELLED'}
        if not out_path.lower().endswith(".png"):
            out_path += ".png"

        img.save(out_path, "PNG")

        # Сохранение отдельных PNG
        if pba.per_object_pngs:
            subdir = os.path.splitext(out_path)[0] + "_objects"
            os.makedirs(subdir, exist_ok=True)
            for item in layout_items:
                sub_img = item.get("_sub_img")
                if sub_img is None:
                    continue
                safe_name = "".join(
                    c if c.isalnum() or c in "._-" else "_"
                    for c in item["obj"].name)
                sub_path = os.path.join(subdir, f"{safe_name}.png")
                sub_img.save(sub_path, "PNG")

        meta_path = os.path.splitext(out_path)[0] + ".uv.json"
        metadata = {
            "format": "pba_uv_layout",
            "version": "1.1",
            "canvas_px": [canvas_w, canvas_h],
            "px_per_meter": px_per_meter,
            "margin_px": margin_px,
            "gap_px": gap_px,
            "scale": f"{pba.scale_num}:{pba.scale_den}",
            "dpi": pba.dpi,
            "objects": obj_meta,
            "skipped": [{"name": n, "reason": r} for n, r in skipped],
        }
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        total_markers = sum(it.get("n_markers", 0) for it in layout_items)
        _set_status(
            scene,
            f"Экспортировано объектов: {len(obj_meta)}, "
            f"пропущено: {len(skipped)}, "
            f"маркеров: {total_markers}. "
            f"Полотно: {canvas_w}×{canvas_h} px")
        self.report({'INFO'},
                    f"{len(obj_meta)} объектов, {canvas_w}×{canvas_h}")
        return {'FINISHED'}


class PBA_UVE_OT_assign_uuids(bpy.types.Operator):
    bl_idname = "pba_uv_export.assign_uuids"
    bl_label = "Assign Vertex UUIDs"
    bl_description = ("Назначить уникальные ID всем вершинам "
                      "выделенных mesh-объектов. Существующие ID "
                      "сохраняются, нули и дубликаты получают "
                      "свежие.")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        objs = [o for o in context.selected_objects if o.type == 'MESH']
        if not objs:
            act = context.active_object
            if act is not None and act.type == 'MESH':
                objs = [act]
        if not objs:
            self.report({'ERROR'}, "Выделите mesh-объекты")
            return {'CANCELLED'}

        # Глобальный максимум по всем объектам — избегаем коллизий
        global_max = 0
        for obj in objs:
            attr = get_uuid_attribute(obj.data, create=False)
            if attr is None:
                continue
            for i in range(len(attr.data)):
                v = int(attr.data[i].value)
                if v > global_max:
                    global_max = v

        total_new = 0
        total_dupes = 0
        total_kept = 0
        seen = set()

        for obj in objs:
            me = obj.data
            attr = get_uuid_attribute(me, create=False)

            # Если атрибут есть, но пустой — пересоздать
            if attr is not None and len(attr.data) == 0:
                me.attributes.remove(attr)
                attr = None

            if attr is None:
                attr = get_uuid_attribute(me, create=True)

            n = len(me.vertices)
            for i in range(n):
                v = int(attr.data[i].value)
                if v == 0:
                    global_max += 1
                    attr.data[i].value = global_max
                    seen.add(global_max)
                    total_new += 1
                elif v in seen:
                    global_max += 1
                    attr.data[i].value = global_max
                    seen.add(global_max)
                    total_dupes += 1
                else:
                    seen.add(v)
                    total_kept += 1
            me.update()

        parts = []
        if total_new:
            parts.append(f"{total_new} новых")
        if total_dupes:
            parts.append(f"{total_dupes} дубликатов")
        if total_kept:
            parts.append(f"{total_kept} без изменений")
        msg = f"UUID: {', '.join(parts)}. Объектов: {len(objs)}"
        scene.pba_uv_exporter.uuid_status_msg = msg
        print(f"[PBA UVE] {msg}")
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class PBA_UVE_OT_check_uuids(bpy.types.Operator):
    bl_idname = "pba_uv_export.check_uuids"
    bl_label = "Check UUIDs"
    bl_description = ("Проверить состояние UUID у выделенных "
                      "или всех mesh-объектов")
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        objs = [o for o in context.selected_objects if o.type == 'MESH']
        if not objs:
            objs = [o for o in scene.objects if o.type == 'MESH']
        if not objs:
            self.report({'WARNING'}, "Нет mesh-объектов")
            return {'CANCELLED'}

        total_verts = 0
        total_with_uuid = 0
        total_zero = 0
        all_ids = {}

        print(f"[PBA UVE] Проверка UUID для {len(objs)} объектов:")
        for obj in objs:
            me = obj.data
            attr = get_uuid_attribute(me, create=False)
            n_verts = len(me.vertices)
            n_zero = 0
            n_dupes = 0
            local_seen = {}
            if attr is not None:
                for i in range(len(attr.data)):
                    v = int(attr.data[i].value)
                    if v == 0:
                        n_zero += 1
                        continue
                    if v in all_ids and all_ids[v] != obj.name:
                        n_dupes += 1
                    all_ids[v] = obj.name
                    local_seen[v] = local_seen.get(v, 0) + 1

            total_verts += n_verts
            total_with_uuid += n_verts - n_zero
            total_zero += n_zero

            status = "OK"
            if attr is None:
                status = "нет атрибута"
            elif n_zero > 0:
                status = f"{n_zero} нулей"
            elif n_dupes > 0:
                status = f"{n_dupes} дубликатов"

            print(f"  {obj.name}: {n_verts} вершин, "
                  f"UUID {n_verts - n_zero}, "
                  f"нулей {n_zero} → {status}")

        print(f"[PBA UVE] Итого: {total_verts} вершин, "
              f"с UUID {total_with_uuid}, "
              f"без UUID {total_zero}")

        if total_zero > 0:
            msg = (f"Есть {total_zero} вершин без UUID. "
                   f"Выделите объекты и Assign Vertex UUIDs.")
        else:
            msg = f"Все {total_verts} вершин имеют UUID."

        scene.pba_uv_exporter.uuid_status_msg = msg
        self.report({'INFO'}, msg)
        return {'FINISHED'}


classes = (
    PBA_UVE_OT_export,
    PBA_UVE_OT_assign_uuids,
    PBA_UVE_OT_check_uuids,
)
