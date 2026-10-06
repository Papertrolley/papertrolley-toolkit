import json
import math
import os

import bmesh
import bpy
import numpy as np
from mathutils import Vector
from bpy_extras.view3d_utils import (region_2d_to_origin_3d,
                                      region_2d_to_vector_3d)


PLANE_MESH_NAME = "PBA_LayoutMesh"
PLANE_MAT_NAME = "PBA_LayoutMat"


def _set_status(scene, msg):
    scene.pba_lm.status_msg = msg
    print(f"[PBA LM] {msg}")


def _get_plane(scene):
    name = scene.pba_lm.plane_obj_name
    if not name:
        return None
    obj = scene.objects.get(name)
    if obj is None or obj.type != 'MESH':
        return None
    return obj


def _get_layout_image(scene):
    name = scene.pba_lm.layout_image_name
    if not name:
        return None
    return bpy.data.images.get(name)


def _remove_plane(scene):
    obj = _get_plane(scene)
    if obj is not None:
        mesh = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    scene.pba_lm.plane_obj_name = ""


def _create_plane(scene, image, width_m, offset_m):
    W_px, H_px = image.size
    if W_px == 0 or H_px == 0:
        raise ValueError("Изображение пустое")

    aspect = H_px / W_px
    height_m = width_m * aspect
    hw = width_m / 2.0
    hh = height_m / 2.0

    # Плоскость в XZ (y=0), нормаль смотрит в -Y
    verts = [(-hw, 0.0, -hh), (hw, 0.0, -hh),
             (hw, 0.0, hh), (-hw, 0.0, hh)]
    faces = [(0, 1, 2, 3)]

    mesh = bpy.data.meshes.new(PLANE_MESH_NAME)
    mesh.from_pydata(verts, [], faces)
    mesh.update()

    uv_layer = mesh.uv_layers.new(name="UVMap")
    for i, uv in enumerate([(0.0, 0.0), (1.0, 0.0),
                             (1.0, 1.0), (0.0, 1.0)]):
        uv_layer.data[i].uv = uv

    obj = bpy.data.objects.new(PLANE_MESH_NAME, mesh)
    scene.collection.objects.link(obj)
    obj.location = (0.0, -offset_m, 0.0)

    # Материал
    mat = bpy.data.materials.new(PLANE_MAT_NAME)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = image
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.inputs["Alpha"].default_value = 0.5
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    if hasattr(mat, "blend_method"):
        try:
            mat.blend_method = 'BLEND'
        except Exception:
            pass
    mat.diffuse_color = (1.0, 1.0, 1.0, 0.5)
    obj.data.materials.append(mat)

    scene.pba_lm.plane_obj_name = obj.name
    return obj


# --- Сортировка углов ---

def _canonical_order(points_2d):
    """Порядок обхода 4 точек: по часовой стрелке (в системе с y вниз),
    начиная с верхнего-левого."""
    if len(points_2d) != 4:
        return list(range(len(points_2d)))
    cx = sum(p[0] for p in points_2d) / 4.0
    cy = sum(p[1] for p in points_2d) / 4.0

    order = sorted(
        range(4),
        key=lambda i: math.atan2(points_2d[i][1] - cy,
                                  points_2d[i][0] - cx))
    sums = [points_2d[i][0] + points_2d[i][1] for i in range(4)]
    tl_idx = sums.index(min(sums))
    pos = order.index(tl_idx)
    return order[pos:] + order[:pos]


def _get_view_direction(context):
    area = context.area
    if area is None or area.type != 'VIEW_3D':
        return Vector((0.0, 1.0, 0.0))
    rv3d = area.spaces[0].region_3d
    return rv3d.view_rotation @ Vector((0.0, 0.0, -1.0))


def _project_for_sorting(points_world, view_dir):
    """Проецирует 3D точки на плоскость вида, возвращает 2D экранные."""
    vd = np.array(view_dir, dtype=np.float64)
    n = np.linalg.norm(vd)
    if n < 1e-9:
        vd = np.array([0.0, 1.0, 0.0])
    else:
        vd = vd / n

    up_hint = np.array([0.0, 0.0, 1.0])
    if abs(float(np.dot(up_hint, vd))) > 0.95:
        up_hint = np.array([0.0, 1.0, 0.0])
    right = np.cross(vd, up_hint)
    right = right / np.linalg.norm(right)
    up = np.cross(right, vd)
    up = up / np.linalg.norm(up)

    pts_2d = []
    for p in points_world:
        parr = np.array([p[0], p[1], p[2]], dtype=np.float64)
        x_r = float(np.dot(parr, right))
        y_u = float(np.dot(parr, up))
        pts_2d.append((x_r, -y_u))
    return pts_2d


# --- Операторы ---

class PBA_LM_OT_load_layout(bpy.types.Operator):
    bl_idname = "pba_lm.load_layout"
    bl_label = "Load Layout"
    bl_description = "Загрузить PNG выкройки и создать плоскость"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_lm
        path = bpy.path.abspath(pba.layout_path)
        if not path or not os.path.exists(path):
            self.report({'ERROR'}, "Файл не найден")
            return {'CANCELLED'}

        try:
            img = bpy.data.images.load(path, check_existing=True)
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка загрузки: {e}")
            return {'CANCELLED'}

        if img.size[0] == 0 or img.size[1] == 0:
            self.report({'ERROR'}, "Изображение пустое")
            return {'CANCELLED'}

        _remove_plane(scene)
        try:
            _create_plane(scene, img, pba.plane_width_m, pba.plane_offset_m)
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка создания плоскости: {e}")
            return {'CANCELLED'}

        pba.layout_image_name = img.name
        _set_status(
            scene,
            f"Загружено {img.size[0]}×{img.size[1]} px. "
            f"Переключитесь в вид спереди (Numpad 1).")
        self.report({'INFO'}, "Выкройка загружена")
        return {'FINISHED'}


class PBA_LM_OT_capture_3d(bpy.types.Operator):
    bl_idname = "pba_lm.capture_3d"
    bl_label = "Capture 4 3D Vertices"
    bl_description = "Выделите 4 вершины в Edit Mode и нажмите"
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_lm
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            self.report({'ERROR'}, "Нужен активный mesh-объект")
            return {'CANCELLED'}
        if context.mode != 'EDIT_MESH':
            self.report({'ERROR'}, "Нужен Edit Mode")
            return {'CANCELLED'}

        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        selected = [v for v in bm.verts if v.select]
        if len(selected) != 4:
            self.report({'ERROR'}, f"Выделено {len(selected)}, нужно 4")
            return {'CANCELLED'}

        M = obj.matrix_world
        world_pos = [M @ v.co for v in selected]
        view_dir = _get_view_direction(context)
        pts_2d = _project_for_sorting(world_pos, view_dir)
        order = _canonical_order(pts_2d)

        pba.pending_corners_3d.clear()
        for i in order:
            v = selected[i]
            wp = world_pos[i]
            c = pba.pending_corners_3d.add()
            c.obj_name = obj.name
            c.vertex_index = v.index
            c.x_world = (wp.x, wp.y, wp.z)

        ids = [f"v{selected[i].index}" for i in order]
        _set_status(scene, f"3D захвачены: {', '.join(ids)}. "
                           f"Теперь кликните 4 точки на выкройке.")
        self.report({'INFO'}, "4 вершины захвачены")
        return {'FINISHED'}


class PBA_LM_OT_pick_image_points(bpy.types.Operator):
    bl_idname = "pba_lm.pick_image_points"
    bl_label = "Pick 4 Image Points"
    bl_description = "Кликните 4 точки на плоскости выкройки"
    bl_options = {'REGISTER'}

    _clicks = 0

    def invoke(self, context, event):
        scene = context.scene
        pba = scene.pba_lm
        if _get_plane(scene) is None:
            self.report({'ERROR'}, "Плоскость выкройки не создана")
            return {'CANCELLED'}
        if len(pba.pending_corners_3d) != 4:
            self.report({'ERROR'}, "Сначала захватите 4 3D-вершины")
            return {'CANCELLED'}
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'ERROR'}, "Запустите из 3D Viewport")
            return {'CANCELLED'}

        self._clicks = 0
        pba.pending_px_count = 0
        pba.pending_corners_px = (0.0,) * 8

        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set(
            "Кликните 4 точки на выкройке. ESC — отмена.")
        _set_status(scene, "Ожидание точки 1 из 4")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        scene = context.scene
        pba = scene.pba_lm

        if event.type in {'ESC', 'RIGHTMOUSE'}:
            context.workspace.status_text_set(None)
            _set_status(scene, "Отменено")
            return {'CANCELLED'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            coord = (event.mouse_region_x, event.mouse_region_y)
            origin = region_2d_to_origin_3d(
                context.region, context.space_data.region_3d, coord)
            direction = region_2d_to_vector_3d(
                context.region, context.space_data.region_3d, coord)

            plane = _get_plane(scene)
            if plane is None:
                context.workspace.status_text_set(None)
                return {'CANCELLED'}

            inv = plane.matrix_world.inverted()
            o_loc = inv @ origin
            d_loc = inv.to_3x3() @ direction
            hit, loc, _, _ = plane.ray_cast(o_loc, d_loc)

            if not hit:
                return {'RUNNING_MODAL'}

            mesh = plane.data
            xs = [v.co.x for v in mesh.vertices]
            zs = [v.co.z for v in mesh.vertices]
            hw = (max(xs) - min(xs)) / 2.0
            hh = (max(zs) - min(zs)) / 2.0

            u = (loc.x + hw) / (2.0 * hw)
            v = (loc.z + hh) / (2.0 * hh)

            img = _get_layout_image(scene)
            if img is None:
                return {'RUNNING_MODAL'}
            W_px, H_px = img.size
            px = u * W_px
            py = (1.0 - v) * H_px

            arr = list(pba.pending_corners_px)
            arr[self._clicks * 2] = px
            arr[self._clicks * 2 + 1] = py
            pba.pending_corners_px = tuple(arr)
            pba.pending_px_count = self._clicks + 1
            self._clicks += 1

            _set_status(scene,
                        f"Точка {self._clicks}: ({px:.1f}, {py:.1f})")

            if self._clicks >= 4:
                _commit_quad(scene)
                context.workspace.status_text_set(None)
                return {'FINISHED'}

        return {'RUNNING_MODAL'}


def _commit_quad(scene):
    pba = scene.pba_lm
    if len(pba.pending_corners_3d) != 4 or pba.pending_px_count != 4:
        return

    # Канонический порядок изображения
    px = list(pba.pending_corners_px)
    pts_2d = [(px[0], px[1]), (px[2], px[3]),
              (px[4], px[5]), (px[6], px[7])]
    order = _canonical_order(pts_2d)
    pts_sorted = [pts_2d[i] for i in order]

    q = pba.quads.add()
    pba.quad_counter += 1
    q.id = f"q_{pba.quad_counter:03d}"
    q.part = pba.pending_part

    for c in pba.pending_corners_3d:
        nc = q.corners_3d.add()
        nc.obj_name = c.obj_name
        nc.vertex_index = c.vertex_index
        nc.x_world = c.x_world

    flat = []
    for (x, y) in pts_sorted:
        flat.extend([x, y])
    q.corners_px = tuple(flat)

    pba.pending_corners_3d.clear()
    pba.pending_corners_px = (0.0,) * 8
    pba.pending_px_count = 0

    _set_status(scene, f"Квад {q.id} добавлен. "
                       f"Всего квадов: {len(pba.quads)}")


class PBA_LM_OT_cancel_quad(bpy.types.Operator):
    bl_idname = "pba_lm.cancel_quad"
    bl_label = "Cancel Quad"
    bl_options = {'REGISTER'}

    def execute(self, context):
        pba = context.scene.pba_lm
        pba.pending_corners_3d.clear()
        pba.pending_corners_px = (0.0,) * 8
        pba.pending_px_count = 0
        _set_status(context.scene, "Текущий квад сброшен")
        return {'FINISHED'}


class PBA_LM_OT_remove_quad(bpy.types.Operator):
    bl_idname = "pba_lm.remove_quad"
    bl_label = "Remove Quad"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        pba = context.scene.pba_lm
        if 0 <= self.index < len(pba.quads):
            pba.quads.remove(self.index)
        return {'FINISHED'}


class PBA_LM_OT_clear_quads(bpy.types.Operator):
    bl_idname = "pba_lm.clear_quads"
    bl_label = "Clear All Quads"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        pba = context.scene.pba_lm
        pba.quads.clear()
        pba.quad_counter = 0
        _set_status(context.scene, "Все квады удалены")
        return {'FINISHED'}


class PBA_LM_OT_export_json(bpy.types.Operator):
    bl_idname = "pba_lm.export_json"
    bl_label = "Export JSON"
    bl_description = "Сохранить разметку в JSON"
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_lm
        if not pba.quads:
            self.report({'WARNING'}, "Нет размеченных квадов")
            return {'CANCELLED'}

        layout_path = bpy.path.abspath(pba.layout_path)
        if not layout_path:
            self.report({'ERROR'}, "Не задан путь к выкройке")
            return {'CANCELLED'}

        img = _get_layout_image(scene)
        if img is None:
            self.report({'ERROR'}, "Изображение выкройки не загружено")
            return {'CANCELLED'}
        W_px, H_px = img.size
        orientation = "portrait" if H_px > W_px else "landscape"

        quads_data = []
        for q in pba.quads:
            corners_3d = []
            for c in q.corners_3d:
                corners_3d.append({
                    "obj": c.obj_name,
                    "vertex": c.vertex_index,
                    "x_world": [float(c.x_world[0]),
                                 float(c.x_world[1]),
                                 float(c.x_world[2])],
                })
            px = list(q.corners_px)
            corners_px = [[px[0], px[1]], [px[2], px[3]],
                          [px[4], px[5]], [px[6], px[7]]]
            quads_data.append({
                "id": q.id,
                "part": q.part,
                "corners_3d": corners_3d,
                "corners_layout_px": corners_px,
            })

        data = {
            "sheet": os.path.basename(layout_path),
            "size": [W_px, H_px],
            "dpi": 600,
            "orientation": orientation,
            "quads": quads_data,
        }

        out = os.path.splitext(layout_path)[0] + ".quads.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        _set_status(scene, f"Сохранено: {out} ({len(pba.quads)} квадов)")
        self.report({'INFO'}, f"JSON: {out}")
        return {'FINISHED'}


class PBA_LM_OT_frame_layout(bpy.types.Operator):
    bl_idname = "pba_lm.frame_layout"
    bl_label = "Frame Layout View"
    bl_description = "Ортографический вид спереди + Material Preview"
    bl_options = {'REGISTER'}

    def execute(self, context):
        from mathutils import Quaternion, Vector

        scene = context.scene
        plane = _get_plane(scene)
        if plane is None:
            self.report({'ERROR'}, "Плоскость выкройки не создана")
            return {'CANCELLED'}

        # Найти 3D Viewport
        area = None
        if context.area is not None and context.area.type == 'VIEW_3D':
            area = context.area
        else:
            for a in context.screen.areas:
                if a.type == 'VIEW_3D':
                    area = a
                    break

        if area is None:
            self.report({'ERROR'}, "Не найден 3D Viewport")
            return {'CANCELLED'}

        space = area.spaces.active
        rv3d = space.region_3d

        # Режим отображения: Material Preview
        try:
            space.shading.type = 'MATERIAL'
        except Exception as e:
            print(f"[PBA LM] не удалось переключить shading: {e}")

        # Ортографическая проекция
        rv3d.view_perspective = 'ORTHO'

        # Front view: поворот вокруг X на 90°
        rv3d.view_rotation = Quaternion((0.7071068, 0.7071068, 0.0, 0.0))

        # Центр плоскости в мире
        center = plane.matrix_world.translation.copy()
        rv3d.view_location = Vector((center.x, center.y, center.z))

        # Дистанция и масштаб
        pba = scene.pba_lm
        rv3d.view_distance = max(pba.plane_offset_m, 1.0)

        try:
            rv3d.ortho_scale = pba.plane_width_m * 1.1
        except Exception as e:
            print(f"[PBA LM] не удалось задать ortho_scale: {e}")

        _set_status(scene, "Вид переключён: Front, Ortho, Material Preview")
        return {'FINISHED'}


classes = (
    PBA_LM_OT_load_layout,
    PBA_LM_OT_capture_3d,
    PBA_LM_OT_pick_image_points,
    PBA_LM_OT_cancel_quad,
    PBA_LM_OT_remove_quad,
    PBA_LM_OT_clear_quads,
    PBA_LM_OT_export_json,
    PBA_LM_OT_frame_layout,
)
