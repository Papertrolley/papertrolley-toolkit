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
bl_info = {
    "name": "PT Triangulation",
    "author": "PaperTroley",
    "version": (0, 2, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > PT Model",
    "description": "Триангуляция 3D-точек по нескольким калиброванным фото",
    "category": "Modeling",
}

import bpy

from . import ops, panel


class PBA_TRI_Click(bpy.types.PropertyGroup):
    slot: bpy.props.IntProperty(default=-1)
    x2d: bpy.props.FloatVectorProperty(size=2, default=(0.0, 0.0))


class PBA_TRI_Correspondence(bpy.types.PropertyGroup):
    obj_name: bpy.props.StringProperty(default="")
    vertex_index: bpy.props.IntProperty(default=-1)
    vertex_uuid: bpy.props.IntProperty(default=0)
    x2d: bpy.props.FloatVectorProperty(size=2, default=(0.0, 0.0))
    x_world: bpy.props.FloatVectorProperty(size=3, default=(0.0, 0.0, 0.0))
    enabled: bpy.props.BoolProperty(default=True)


class PBA_TRI_Photo(bpy.types.PropertyGroup):
    json_path: bpy.props.StringProperty(subtype='FILE_PATH')
    loaded: bpy.props.BoolProperty(default=False)
    camera_name: bpy.props.StringProperty(default="")
    image_name: bpy.props.StringProperty(default="")
    width_px: bpy.props.IntProperty(default=0)
    height_px: bpy.props.IntProperty(default=0)
    lens_mm: bpy.props.FloatProperty(default=50.0)
    sensor_width_mm: bpy.props.FloatProperty(default=36.0)
    f_px: bpy.props.FloatProperty(default=0.0)
    k1: bpy.props.FloatProperty(default=0.0)
    k2: bpy.props.FloatProperty(default=0.0)
    use_distortion: bpy.props.BoolProperty(default=False)
    use_k2: bpy.props.BoolProperty(default=False)
    matrix_world: bpy.props.FloatVectorProperty(size=16,
                                                default=(0.0,) * 16)
    correspondences: bpy.props.CollectionProperty(
        type=PBA_TRI_Correspondence)


class PBA_TRI_Props(bpy.types.PropertyGroup):
    photos: bpy.props.CollectionProperty(type=PBA_TRI_Photo)
    active_slot: bpy.props.IntProperty(default=0)

    building: bpy.props.BoolProperty(default=False)
    clicks: bpy.props.CollectionProperty(type=PBA_TRI_Click)

    output_object_name: bpy.props.StringProperty(default="")
    point_size: bpy.props.FloatProperty(default=0.02,
                                         min=0.001, max=1.0)

    status_msg: bpy.props.StringProperty(default="")


classes = ((PBA_TRI_Click, PBA_TRI_Correspondence,
            PBA_TRI_Photo, PBA_TRI_Props)
           + ops.classes + panel.classes)
        
        
def _ensure_slots_for_scene(scene):
    p = getattr(scene, "pba_tri", None)
    if p is None:
        return
    while len(p.photos) < 3:
        p.photos.add()


def _init_slots_timer():
    for scene in bpy.data.scenes:
        _ensure_slots_for_scene(scene)
    return None  # не повторять


@bpy.app.handlers.persistent
def _on_load_post(_dummy):
    for scene in bpy.data.scenes:
        _ensure_slots_for_scene(scene)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.pba_tri = bpy.props.PointerProperty(type=PBA_TRI_Props)

    bpy.app.handlers.load_post.append(_on_load_post)
    bpy.app.timers.register(_init_slots_timer, first_interval=0.1)


def unregister():
    try:
        bpy.app.timers.unregister(_init_slots_timer)
    except Exception:
        pass
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)

    del bpy.types.Scene.pba_tri
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
        


if __name__ == "__main__":
    register()
