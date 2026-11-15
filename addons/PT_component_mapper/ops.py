import bpy
import os
import math
import re
import json
from collections import defaultdict
from PIL import Image, ImageDraw
from bpy_extras.io_utils import ExportHelper

PBA_VERTEX_ID_ATTR = "pba_vertex_id"

# ====================== Render constants ======================
RENDER_EDGE_COLOR = (128, 128, 128, 255)
RENDER_EDGE_WIDTH = 1
RENDER_BOUNDARY_COLOR = (0, 0, 0, 255)
RENDER_BOUNDARY_WIDTH = 2
RENDER_VERTEX_COLOR = (255, 0, 255, 255)


class PBA_CM_Props(bpy.types.PropertyGroup):
    active_uv_map: bpy.props.StringProperty(name="UV Map")
    piece_name: bpy.props.StringProperty(name="Piece Name")
    scale_str: bpy.props.StringProperty(name="Scale", default="1:87")
    dpi: bpy.props.IntProperty(name="DPI", default=600)
    margin_px: bpy.props.IntProperty(name="Margin (px)", default=20)
    loaded_component_path: bpy.props.StringProperty(default="")
    loaded_piece_json: bpy.props.StringProperty(default="")
    loaded_piece_summary: bpy.props.StringProperty(default="")
    new_uv_layer_name: bpy.props.StringProperty(name="standard_87-600")
    status_msg: bpy.props.StringProperty()


def _set_status(scene, msg):
    scene.pba_cm.status_msg = msg
    print(f"[PBA CM] {msg}")


def _ensure_object_mode(context):
    if context.mode != 'OBJECT':
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except Exception:
            pass


def _list_uv_maps(scene):
    result = set()
    for obj in scene.objects:
        if obj.type != 'MESH':
            continue
        for layer in obj.data.uv_layers:
            result.add(layer.name)
    return sorted(result)


def _parse_uv_name(name):
    """skleyka_64-600 → ('skleyka', 64, 600) | None."""
    if '_' not in name:
        return None
    base, tail = name.rsplit('_', 1)
    if '-' not in tail:
        return None
    parts = tail.split('-')
    if len(parts) != 2:
        return None
    try:
        scale = int(parts[0])
        dpi = int(parts[1])
    except ValueError:
        return None
    if not base or scale <= 0 or dpi <= 0:
        return None
    return base, scale, dpi


def _parse_scale(scale_str):
    try:
        return float(scale_str.split(":")[1])
    except Exception:
        return 87.0


def _ensure_local_uuids(obj):
    me = obj.data
    attr = me.attributes.get(PBA_VERTEX_ID_ATTR)
    if attr is None:
        attr = me.attributes.new(
            name=PBA_VERTEX_ID_ATTR, type='INT', domain='POINT')
        for i in range(len(me.vertices)):
            attr.data[i].value = i + 1
        return
    max_id = 0
    for i in range(len(attr.data)):
        v = int(attr.data[i].value)
        if v > max_id:
            max_id = v
    next_id = max_id + 1
    seen = set()
    for i in range(len(attr.data)):
        v = int(attr.data[i].value)
        if v > 0 and v not in seen:
            seen.add(v)
            continue
        attr.data[i].value = next_id
        seen.add(next_id)
        next_id += 1


def _get_vertex_uuid(mesh, vi):
    attr = mesh.attributes.get(PBA_VERTEX_ID_ATTR)
    if attr is None or vi < 0 or vi >= len(attr.data):
        return 0
    return int(attr.data[vi].value)


def _compute_meters_per_uv(scene, uv_map_name):
    total_3d = total_uv = 0.0
    for obj in scene.objects:
        if obj.type != 'MESH':
            continue
        me = obj.data
        uv_layer = me.uv_layers.get(uv_map_name)
        if uv_layer is None or len(uv_layer.data) == 0:
            continue
        for poly in me.polygons:
            verts = [me.vertices[me.loops[li].vertex_index].co
                     for li in poly.loop_indices]
            uvs = [uv_layer.data[li].uv for li in poly.loop_indices]
            for i in range(1, len(verts) - 1):
                total_3d += (verts[i] - verts[0]).cross(
                    verts[i + 1] - verts[0]).length * 0.5
                total_uv += abs(
                    (uvs[i].x - uvs[0].x) * (uvs[i + 1].y - uvs[0].y)
                    - (uvs[i + 1].x - uvs[0].x) * (uvs[i].y - uvs[0].y)
                ) * 0.5
    if total_uv < 1e-12:
        return None
    return math.sqrt(total_3d / total_uv)


def _find_islands(mesh, uv_map_name):
    uv_layer = mesh.uv_layers.get(uv_map_name)
    if uv_layer is None:
        return []
    loop_map = defaultdict(list)
    for poly in mesh.polygons:
        for li in poly.loop_indices:
            vi = mesh.loops[li].vertex_index
            uv = uv_layer.data[li].uv
            key = (vi, round(uv.x, 6), round(uv.y, 6))
            loop_map[key].append(poly.index)
    visited = set()
    islands = []
    for poly in mesh.polygons:
        if poly.index in visited:
            continue
        queue = [poly.index]
        visited.add(poly.index)
        island = []
        while queue:
            fi = queue.pop(0)
            island.append(fi)
            for li in mesh.polygons[fi].loop_indices:
                vi = mesh.loops[li].vertex_index
                uv = uv_layer.data[li].uv
                key = (vi, round(uv.x, 6), round(uv.y, 6))
                for other in loop_map[key]:
                    if other not in visited:
                        visited.add(other)
                        queue.append(other)
        islands.append(sorted(island))
    islands.sort(key=lambda isl: min(isl))
    return islands


def _island_uv_bbox(mesh, uv_map_name, island_faces):
    uv_layer = mesh.uv_layers[uv_map_name]
    u_min = v_min = float('inf')
    u_max = v_max = float('-inf')
    for fi in island_faces:
        for li in mesh.polygons[fi].loop_indices:
            uv = uv_layer.data[li].uv
            u_min = min(u_min, uv.x); u_max = max(u_max, uv.x)
            v_min = min(v_min, uv.y); v_max = max(v_max, uv.y)
    return u_min, v_min, u_max, v_max


def _edge_uv_key(va, vb, uv_a, uv_b):
    if va < vb:
        return (va, vb,
                round(uv_a[0], 6), round(uv_a[1], 6),
                round(uv_b[0], 6), round(uv_b[1], 6))
    return (vb, va,
            round(uv_b[0], 6), round(uv_b[1], 6),
            round(uv_a[0], 6), round(uv_a[1], 6))


def _build_uv_edge_counts(mesh, uv_map_name):
    uv_layer = mesh.uv_layers.get(uv_map_name)
    if uv_layer is None:
        return {}
    counts = defaultdict(int)
    for poly in mesh.polygons:
        verts = poly.vertices
        loops = poly.loop_indices
        n = len(verts)
        for i in range(n):
            va = int(verts[i])
            vb = int(verts[(i + 1) % n])
            li_a = loops[i]
            li_b = loops[(i + 1) % n]
            uv_a = uv_layer.data[li_a].uv
            uv_b = uv_layer.data[li_b].uv
            key = _edge_uv_key(va, vb,
                                (uv_a.x, uv_a.y),
                                (uv_b.x, uv_b.y))
            counts[key] += 1
    return counts


def _find_sidecar_json(comp_dir, uv_map_name):
    """Ищет pieces/*.json по полю uv_map."""
    pieces_dir = os.path.join(comp_dir, "pieces")
    if not os.path.isdir(pieces_dir):
        return None, None
    for root, _dirs, files in os.walk(pieces_dir):
        for f in files:
            if not f.endswith(".json"):
                continue
            path = os.path.join(root, f)
            try:
                with open(path, 'r', encoding='utf-8') as fp:
                    data = json.load(fp)
            except Exception:
                continue
            if data.get("uv_map") == uv_map_name:
                return path, data
    return None, None


def _save_objects_to_blend(objects, filepath):
    datablocks = set()
    for obj in objects:
        if obj.type != 'MESH':
            continue
        datablocks.add(obj)
        try:
            obj.use_fake_user = True
        except Exception:
            pass
        if obj.data is not None:
            datablocks.add(obj.data)
            try:
                obj.data.use_fake_user = True
                obj.data.update()
            except Exception:
                pass
            for slot in obj.material_slots:
                if slot.material is not None:
                    datablocks.add(slot.material)
                    try:
                        slot.material.use_fake_user = True
                    except Exception:
                        pass
    if not datablocks:
        raise ValueError("Нет mesh-объектов")
    bpy.data.libraries.write(filepath, datablocks, fake_user=True)


def _clear_mesh_objects(context):
    count = 0
    for obj in list(bpy.data.objects):
        if obj.type in ('CAMERA', 'LIGHT'):
            continue
        bpy.data.objects.remove(obj, do_unlink=True)
        count += 1
    for mesh in list(bpy.data.meshes):
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    for mat in list(bpy.data.materials):
        if mat.users == 0:
            bpy.data.materials.remove(mat)
    return count


# ====================== Operators ======================

class PBA_CM_OT_load_component(bpy.types.Operator):
    bl_idname = "pba_cm.load_component"
    bl_label = "Load Component"
    bl_description = ("Открыть mesh.blend компонента как рабочий файл. "
                      "Старые файлы без сцены мигрируются автоматически.")
    bl_options = {'REGISTER'}

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(
        default="*.blend", options={'HIDDEN'})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        path = bpy.path.abspath(self.filepath)
        if not path or not os.path.exists(path):
            self.report({'ERROR'}, "Файл не найден")
            return {'CANCELLED'}
        if bpy.data.filepath == path:
            return {'FINISHED'}

        try:
            bpy.ops.wm.open_mainfile(filepath=path)
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка открытия: {e}")
            return {'CANCELLED'}

        scene = bpy.context.scene
        pba = scene.pba_cm
        pba.loaded_component_path = path

        mesh_in_scene = [o for o in scene.objects if o.type == 'MESH']
        if not mesh_in_scene:
            orphans = [o for o in bpy.data.objects if o.type == 'MESH']
            if orphans:
                for obj in orphans:
                    scene.collection.objects.link(obj)
                _set_status(
                    scene,
                    f"Миграция: {len(orphans)} объектов добавлены в сцену. "
                    f"Нажмите Save Component для сохранения.")
                return {'FINISHED'}

        uv_maps = _list_uv_maps(scene)
        if uv_maps:
            chosen = next(
                (n for n in uv_maps if _parse_uv_name(n)), uv_maps[0])
            bpy.ops.pba_cm.set_uv_map(uv_map_name=chosen)

        _set_status(scene,
                    f"Открыт компонент: {os.path.basename(path)}")
        return {'FINISHED'}


class PBA_CM_OT_set_uv_map(bpy.types.Operator):
    bl_idname = "pba_cm.set_uv_map"
    bl_label = "Set UV Map"
    bl_options = {'INTERNAL'}

    uv_map_name: bpy.props.StringProperty()

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_cm
        pba.active_uv_map = self.uv_map_name
        pba.loaded_piece_json = ""
        pba.loaded_piece_summary = ""

        parsed = _parse_uv_name(self.uv_map_name)
        if parsed:
            piece, scale, dpi = parsed
            pba.piece_name = piece
            pba.scale_str = f"1:{scale}"
            pba.dpi = dpi

        for obj in scene.objects:
            if obj.type != 'MESH':
                continue
            layer = obj.data.uv_layers.get(self.uv_map_name)
            if layer is None:
                continue
            obj.data.uv_layers.active = layer

        if pba.loaded_component_path:
            comp_dir = os.path.dirname(pba.loaded_component_path)
            json_path, data = _find_sidecar_json(
                comp_dir, self.uv_map_name)
            if json_path:
                pba.loaded_piece_json = json_path
                n_islands = len(data.get("islands", []))
                sz = data.get("size_px", [0, 0])
                pba.loaded_piece_summary = (
                    f"Sidecar: {os.path.basename(json_path)} — "
                    f"{n_islands} островов, {sz[0]}×{sz[1]} px")
                _set_status(
                    scene,
                    f"UV '{self.uv_map_name}': sidecar загружен")
            else:
                _set_status(
                    scene,
                    f"UV '{self.uv_map_name}': sidecar не найден "
                    f"(будет создан при Save)")
        else:
            _set_status(
                scene,
                f"UV '{self.uv_map_name}' выбран")
        return {'FINISHED'}


class PBA_CM_OT_save_piece(bpy.types.Operator, ExportHelper):
    bl_idname = "pba_cm.save_piece"
    bl_label = "Save Piece"
    bl_description = "Экспортировать UV-раскладку в PNG + JSON"

    filename_ext = ".png"
    filter_glob: bpy.props.StringProperty(
        default="*.png", options={'HIDDEN'})

    def invoke(self, context, event):
        scene = context.scene
        pba = scene.pba_cm
        if not pba.active_uv_map:
            self.report({'ERROR'}, "Не выбран UV Map")
            return {'CANCELLED'}
        if not pba.loaded_component_path:
            self.report({'ERROR'}, "Компонент не загружен")
            return {'CANCELLED'}

        comp_dir = os.path.dirname(pba.loaded_component_path)
        pieces_dir = os.path.join(comp_dir, "pieces")
        try:
            os.makedirs(pieces_dir, exist_ok=True)
        except Exception as e:
            self.report({'ERROR'}, f"Не удалось создать {pieces_dir}: {e}")
            return {'CANCELLED'}

        piece = pba.piece_name.strip() or "piece"
        try:
            scale_den = int(_parse_scale(pba.scale_str))
        except Exception:
            scale_den = 87
        safe_piece = re.sub(r'[^A-Za-z0-9._-]', '_', piece)
        default_name = f"{safe_piece}_{scale_den}-{pba.dpi}.png"
        self.filepath = os.path.join(pieces_dir, default_name)

        return ExportHelper.invoke(self, context, event)

    def draw(self, context):
        layout = self.layout
        pba = context.scene.pba_cm
        layout.label(text="Piece parameters:")
        layout.prop(pba, "piece_name", text="Name")
        row = layout.row(align=True)
        row.prop(pba, "scale_str", text="Scale")
        row.prop(pba, "dpi", text="DPI")
        layout.prop(pba, "margin_px", text="Margin (px)")

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_cm
        _ensure_object_mode(context)

        # === СИНХРОНИЗАЦИЯ: сначала .blend, потом экспорт ===
        if not bpy.data.filepath:
            self.report({'ERROR'},
                        "Файл не сохранён. Используйте Load Component.")
            return {'CANCELLED'}

        comp_dir = os.path.dirname(bpy.data.filepath)
        if (pba.loaded_component_path
                and bpy.path.abspath(pba.loaded_component_path)
                    != bpy.data.filepath):
            print(f"[PBA CM] ВНИМАНИЕ: текущий .blend "
                  f"({bpy.data.filepath}) не совпадает с "
                  f"loaded_component_path "
                  f"({pba.loaded_component_path}). Сохраняем текущий.")

        try:
            bpy.ops.wm.save_mainfile()
        except Exception as e:
            self.report({'ERROR'}, f"Не удалось сохранить .blend: {e}")
            return {'CANCELLED'}
        print(f"[PBA CM] .blend сохранён: {bpy.data.filepath}")

        uv_map = pba.active_uv_map.strip()
        if not uv_map:
            self.report({'ERROR'}, "Не выбран UV Map")
            return {'CANCELLED'}

        scale_str = pba.scale_str.strip()
        if ":" not in scale_str:
            self.report({'ERROR'}, "Scale в формате '1:87'")
            return {'CANCELLED'}
        scale_den = _parse_scale(scale_str)
        dpi = pba.dpi

        out_path = bpy.path.abspath(self.filepath)
        if not out_path.lower().endswith(".png"):
            out_path += ".png"
        out_dir = os.path.dirname(out_path)
        if out_dir and not os.path.isdir(out_dir):
            try:
                os.makedirs(out_dir, exist_ok=True)
            except Exception as e:
                self.report({'ERROR'}, f"Не удалось создать: {e}")
                return {'CANCELLED'}

        mesh_objects_all = [o for o in scene.objects if o.type == 'MESH']
        objects_with = [o for o in mesh_objects_all
                        if o.data.uv_layers.get(uv_map) is not None]
        objects_without = [o for o in mesh_objects_all
                           if o.data.uv_layers.get(uv_map) is None]

        if objects_without:
            names = [o.name for o in objects_without]
            print(f"[PBA CM] ВНИМАНИЕ: UV '{uv_map}' отсутствует у "
                  f"{len(names)} объектов: {names[:5]}"
                  f"{'...' if len(names) > 5 else ''}")
            print(f"[PBA CM] Эти объекты НЕ попадут в piece. "
                  f"Используйте 'Sync UV Layers', чтобы добавить слой.")

        if not objects_with:
            self.report({'ERROR'},
                        f"Ни у одного объекта нет UV '{uv_map}'")
            return {'CANCELLED'}

        # 1. Обеспечить UUID на всех meshes
        for obj in scene.objects:
            if obj.type != 'MESH':
                continue
            if obj.data.uv_layers.get(uv_map) is None:
                continue
            _ensure_local_uuids(obj)

        mpu = _compute_meters_per_uv(scene, uv_map)
        if mpu is None:
            self.report({'ERROR'}, "Не удалось вычислить метры/UV")
            return {'CANCELLED'}

        px_per_meter = (1000.0 / scale_den) * (dpi / 25.4)
        px_per_uv = mpu * px_per_meter

        all_islands = []
        u_min = v_min = float('inf')
        u_max = v_max = float('-inf')
        for obj in scene.objects:
            if obj.type != 'MESH':
                continue
            if obj.data.uv_layers.get(uv_map) is None:
                continue
            for island in _find_islands(obj.data, uv_map):
                b = _island_uv_bbox(obj.data, uv_map, island)
                all_islands.append({
                    "obj": obj, "island": island, "bbox": b,
                })
                u_min = min(u_min, b[0]); u_max = max(u_max, b[2])
                v_min = min(v_min, b[1]); v_max = max(v_max, b[3])

        if not all_islands or not math.isfinite(u_min):
            self.report({'ERROR'}, "Острова не найдены")
            return {'CANCELLED'}

        du, dv = u_max - u_min, v_max - v_min
        if du <= 0 or dv <= 0:
            self.report({'ERROR'}, "Нулевой UV bbox")
            return {'CANCELLED'}

        m = pba.margin_px
        W = int(math.ceil(du * px_per_uv + 2 * m))
        H = int(math.ceil(dv * px_per_uv + 2 * m))
        if W < 10 or H < 10 or W > 30000 or H > 30000:
            self.report({'ERROR'}, f"Некорректный размер: {W}×{H}")
            return {'CANCELLED'}

        def uv_to_px(u, v):
            return ((u - u_min) * px_per_uv + m,
                    (v_max - v) * px_per_uv + m)

        # ====================== RENDER ======================
        img = Image.new("RGBA", (W, H), (255, 255, 255, 255))
        draw = ImageDraw.Draw(img)

        edge_counts_cache = {}

        def _get_uv_edge_counts(mesh):
            cache_key = (mesh.name, uv_map)
            if cache_key not in edge_counts_cache:
                edge_counts_cache[cache_key] = _build_uv_edge_counts(
                    mesh, uv_map)
            return edge_counts_cache[cache_key]

        # 1. Рёбра
        for item in all_islands:
            mesh = item["obj"].data
            uv_layer = mesh.uv_layers[uv_map]
            counts = _get_uv_edge_counts(mesh)
            internal_segs = set()
            boundary_segs = set()

            for fi in item["island"]:
                poly = mesh.polygons[fi]
                verts = poly.vertices
                loops = poly.loop_indices
                n = len(verts)
                for i in range(n):
                    va = int(verts[i])
                    vb = int(verts[(i + 1) % n])
                    li_a = loops[i]
                    li_b = loops[(i + 1) % n]
                    uv1 = uv_layer.data[li_a].uv
                    uv2 = uv_layer.data[li_b].uv
                    p1 = uv_to_px(uv1.x, uv1.y)
                    p2 = uv_to_px(uv2.x, uv2.y)
                    seg = (round(p1[0], 1), round(p1[1], 1),
                           round(p2[0], 1), round(p2[1], 1))
                    key = _edge_uv_key(va, vb,
                                        (uv1.x, uv1.y),
                                        (uv2.x, uv2.y))
                    if counts.get(key, 0) <= 1:
                        boundary_segs.add(seg)
                    else:
                        internal_segs.add(seg)

            for x1, y1, x2, y2 in internal_segs:
                draw.line([(x1, y1), (x2, y2)],
                          fill=RENDER_EDGE_COLOR,
                          width=RENDER_EDGE_WIDTH)
            for x1, y1, x2, y2 in boundary_segs:
                draw.line([(x1, y1), (x2, y2)],
                          fill=RENDER_BOUNDARY_COLOR,
                          width=RENDER_BOUNDARY_WIDTH)

        # 2. Вершины
        for item in all_islands:
            mesh = item["obj"].data
            uv_layer = mesh.uv_layers[uv_map]
            seen = set()
            for fi in item["island"]:
                for li in mesh.polygons[fi].loop_indices:
                    vi = mesh.loops[li].vertex_index
                    uv = uv_layer.data[li].uv
                    key = (vi, round(uv.x, 6), round(uv.y, 6))
                    if key in seen:
                        continue
                    seen.add(key)
                    px, py = uv_to_px(uv.x, uv.y)
                    ix = int(round(px))
                    iy = int(round(py))
                    if 0 <= ix < W and 0 <= iy < H:
                        draw.point((ix, iy), fill=RENDER_VERTEX_COLOR)

        img.save(out_path, "PNG")

        # ====================== JSON SIDECAR ======================
        island_counters = defaultdict(int)
        islands_data = []
        for item in all_islands:
            mesh = item["obj"].data
            obj_name = item["obj"].name
            idx = island_counters[obj_name]
            island_counters[obj_name] += 1

            attr = mesh.attributes.get(PBA_VERTEX_ID_ATTR)
            b = item["bbox"]
            corners_uv = [[b[0], b[1]], [b[2], b[1]],
                          [b[2], b[3]], [b[0], b[3]]]
            corners_px = [list(uv_to_px(u, v)) for u, v in corners_uv]

            vertices = []
            seen_v = set()
            for fi in item["island"]:
                for li in mesh.polygons[fi].loop_indices:
                    vi = mesh.loops[li].vertex_index
                    if vi in seen_v:
                        continue
                    seen_v.add(vi)
                    local_uuid = (int(attr.data[vi].value)
                                  if attr else 0)
                    vertices.append({
                        "vertex_index": vi,
                        "local_uuid": local_uuid,
                    })

            islands_data.append({
                "island_id": f"{obj_name}#{idx}",
                "obj_name": obj_name,
                "corners_uv": corners_uv,
                "corners_piece_px": corners_px,
                "vertices": vertices,
            })

        piece_id = os.path.splitext(os.path.basename(out_path))[0]
        json_data = {
            "piece_id": piece_id,
            "uv_map": uv_map,
            "scale": scale_str,
            "scale_den": int(scale_den),
            "dpi": dpi,
            "margin_px": m,
            "size_px": [W, H],
            "uv_bbox": [u_min, v_min, u_max, v_max],
            "px_per_meter": px_per_meter,
            "px_per_uv": px_per_uv,
            "meters_per_uv": mpu,
            "islands": islands_data,
        }
        json_path = os.path.splitext(out_path)[0] + ".json"
        try:
            with open(json_path, 'w', encoding='utf-8') as fp:
                json.dump(json_data, fp, ensure_ascii=False, indent=2)
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка записи JSON: {e}")
            return {'CANCELLED'}

        pba.loaded_piece_json = json_path
        pba.loaded_piece_summary = (
            f"Sidecar: {os.path.basename(json_path)} — "
            f"{len(islands_data)} островов, {W}×{H} px")

        _set_status(
            scene,
            f"Сохранено: {os.path.basename(out_path)} + JSON "
            f"+ .blend ({W}×{H} px, {len(islands_data)} островов)")
        self.report({'INFO'}, f"Piece: {out_path}")
        return {'FINISHED'}


class PBA_CM_OT_save_component(bpy.types.Operator):
    bl_idname = "pba_cm.save_component"
    bl_label = "Save Component (.blend)"
    bl_description = "Сохранить .blend компонента (без PNG/JSON)"
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        if not bpy.data.filepath:
            self.report({'ERROR'},
                        "Файл не сохранён. Используйте Load Component "
                        "или File → Save As.")
            return {'CANCELLED'}

        try:
            bpy.ops.wm.save_mainfile()
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка сохранения: {e}")
            return {'CANCELLED'}

        _set_status(scene,
                    f"Сохранён .blend: "
                    f"{os.path.basename(bpy.data.filepath)}")
        return {'FINISHED'}


class PBA_CM_OT_create_uv_layer(bpy.types.Operator):
    bl_idname = "pba_cm.create_uv_layer"
    bl_label = "Create UV Layer"
    bl_description = "Создать UV-слой у всех mesh-объектов сцены"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_cm
        name = pba.new_uv_layer_name.strip()
        if not name:
            self.report({'ERROR'}, "Укажите имя UV-слоя")
            return {'CANCELLED'}

        mesh_objs = [o for o in scene.objects if o.type == 'MESH']
        if not mesh_objs:
            self.report({'ERROR'}, "Нет mesh-объектов")
            return {'CANCELLED'}

        # Уже есть у кого-то?
        existing_anywhere = any(
            obj.data.uv_layers.get(name) is not None
            for obj in mesh_objs
        )
        if existing_anywhere:
            self.report({'ERROR'}, f"UV-слой '{name}' уже существует")
            return {'CANCELLED'}

        created = 0
        for obj in mesh_objs:
            obj.data.uv_layers.new(name=name)
            created += 1

        pba.new_uv_layer_name = ""
        _set_status(scene,
                    f"Создан UV-слой '{name}' у {created} объектов")
        return {'FINISHED'}


class PBA_CM_OT_delete_uv_layer(bpy.types.Operator):
    bl_idname = "pba_cm.delete_uv_layer"
    bl_label = "Delete UV Layer"
    bl_description = "Удалить UV-слой у всех mesh-объектов сцены"
    bl_options = {'REGISTER', 'UNDO'}

    uv_layer_name: bpy.props.StringProperty()

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_cm
        name = self.uv_layer_name
        if not name:
            return {'CANCELLED'}

        mesh_objs = [o for o in scene.objects if o.type == 'MESH']
        deleted = 0
        for obj in mesh_objs:
            layer = obj.data.uv_layers.get(name)
            if layer is None:
                continue
            obj.data.uv_layers.remove(layer)
            deleted += 1

        # Если удалили активный — сбросить
        if pba.active_uv_map == name:
            pba.active_uv_map = ""
            pba.loaded_piece_json = ""
            pba.loaded_piece_summary = ""

        _set_status(scene,
                    f"Удалён UV-слой '{name}' у {deleted} объектов")
        return {'FINISHED'}


class PBA_CM_OT_validate_uv_layers(bpy.types.Operator):
    bl_idname = "pba_cm.validate_uv_layers"
    bl_label = "Validate UV Layers"
    bl_description = "Проверить, что все объекты имеют одинаковые UV-слои"
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        mesh_objs = [o for o in scene.objects if o.type == 'MESH']
        if len(mesh_objs) < 2:
            _set_status(scene, "Меньше 2 объектов")
            return {'FINISHED'}

        per_obj = {o.name: set(l.name for l in o.data.uv_layers)
                   for o in mesh_objs}
        all_names = set()
        for names in per_obj.values():
            all_names |= names

        missing = {}
        for name in all_names:
            miss = [obj for obj, names in per_obj.items()
                    if name not in names]
            if miss:
                missing[name] = miss

        if not missing:
            _set_status(
                scene,
                f"OK: {len(mesh_objs)} объектов, "
                f"UV-слои: {sorted(all_names)}")
            return {'FINISHED'}

        for name, miss in missing.items():
            preview = miss[:5] + (["..."] if len(miss) > 5 else [])
            print(f"[PBA CM] '{name}' отсутствует у {len(miss)}: {preview}")
        _set_status(
            scene,
            f"Не унифицировано: {len(missing)} UV-слоёв "
            f"(см. консоль)")
        return {'FINISHED'}


classes = (
    PBA_CM_Props,
    PBA_CM_OT_load_component,
    PBA_CM_OT_set_uv_map,
    PBA_CM_OT_save_piece,
    PBA_CM_OT_save_component,
    PBA_CM_OT_create_uv_layer,
    PBA_CM_OT_delete_uv_layer,
    PBA_CM_OT_validate_uv_layers
)
