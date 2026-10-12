bl_info = {
    "name": "PT Layout Mapper",
    "author": "PaperTrolley",
    "version": (0, 1, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > PT Model",
    "description": "Разметка соответствий между 4 вершинами 3D-модели "
                   "и 4 точками на выкройке",
    "category": "Modeling",
}

import bpy

from . import ops, panel


class PBA_LM_Corner3D(bpy.types.PropertyGroup):
    obj_name: bpy.props.StringProperty(default="")
    vertex_index: bpy.props.IntProperty(default=-1)
    x_world: bpy.props.FloatVectorProperty(size=3, default=(0.0, 0.0, 0.0))


class PBA_LM_Quad(bpy.types.PropertyGroup):
    id: bpy.props.StringProperty(default="")
    part: bpy.props.StringProperty(default="")
    corners_3d: bpy.props.CollectionProperty(type=PBA_LM_Corner3D)
    corners_px: bpy.props.FloatVectorProperty(size=8, default=(0.0,) * 8)


class PBA_LM_Props(bpy.types.PropertyGroup):
    layout_path: bpy.props.StringProperty(subtype='FILE_PATH')
    layout_image_name: bpy.props.StringProperty(default="")
    plane_width_m: bpy.props.FloatProperty(
        name="Ширина, м", default=20.0, min=0.5, max=200.0)
    plane_offset_m: bpy.props.FloatProperty(
        name="Отступ, м", default=15.0, min=0.5, max=200.0)
    plane_obj_name: bpy.props.StringProperty(default="")

    pending_corners_3d: bpy.props.CollectionProperty(type=PBA_LM_Corner3D)
    pending_corners_px: bpy.props.FloatVectorProperty(size=8,
                                                      default=(0.0,) * 8)
    pending_px_count: bpy.props.IntProperty(default=0)
    pending_part: bpy.props.StringProperty(default="")

    quads: bpy.props.CollectionProperty(type=PBA_LM_Quad)
    quad_counter: bpy.props.IntProperty(default=0)

    status_msg: bpy.props.StringProperty(default="")
    snap_to_lines: bpy.props.BoolProperty(
        name="Snap к линиям",
        description="При клике автоматически притягивать к ближайшей "
                    "линии выкройки",
        default=True)
    snap_radius_px: bpy.props.IntProperty(
        name="Радиус, px", default=15, min=3, max=60)
    snap_threshold: bpy.props.IntProperty(
        name="Порог яркости", default=180, min=20, max=255)
    snap_marker_tolerance: bpy.props.FloatProperty(
        name="Порог маркера",
        description="Насколько близко к #FF00FF должен быть пиксель "
                    "(0.05 — очень строго, 0.5 — лояльно)",
        default=0.30, min=0.05, max=1.0)
    active_quad_index: bpy.props.IntProperty(default=-1)
    highlight_size: bpy.props.FloatProperty(
        name="Размер маркеров, м", default=0.10,
        min=0.01, max=1.0)
    highlight_layout_obj: bpy.props.StringProperty(default="")
    highlight_model_obj: bpy.props.StringProperty(default="")


classes = ((PBA_LM_Corner3D, PBA_LM_Quad, PBA_LM_Props)
           + ops.classes + panel.classes)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.pba_lm = bpy.props.PointerProperty(type=PBA_LM_Props)


def unregister():
    del bpy.types.Scene.pba_lm
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
