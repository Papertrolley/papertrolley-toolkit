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
    "name": "PT scaled UV",
    "author": "PaperTrolley",
    "version": (0, 3, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > PT scaled UV",
    "description": "Калиброванный экспорт UV-развёртки для 1:87",
    "category": "Modeling",
}

import bpy

from . import ops, panel


class PBA_UVE_Props(bpy.types.PropertyGroup):
    scale_num: bpy.props.IntProperty(name="Scale numerator", default=1)
    scale_den: bpy.props.IntProperty(name="Scale denominator", default=87)
    dpi: bpy.props.IntProperty(name="DPI", default=600)
    margin_mm: bpy.props.FloatProperty(name="Margin, mm", default=5.0)
    gap_mm: bpy.props.FloatProperty(name="Gap, mm", default=10.0)
    wrap_width_px: bpy.props.IntProperty(name="Wrap width, px", default=0)
    min_object_px: bpy.props.IntProperty(name="Min object, px", default=30)
    debug_labels: bpy.props.BoolProperty(
        name="Debug labels", default=False)
    per_object_pngs: bpy.props.BoolProperty(
        name="Per-object PNGs", default=False)

    line_width_px: bpy.props.IntProperty(name="Line width", default=1)
    draw_line_color: bpy.props.FloatVectorProperty(
        name="Line color", size=3, default=(0.0, 0.0, 0.0),
        subtype='COLOR')

    marker_color: bpy.props.FloatVectorProperty(
        name="Marker color", size=3, default=(1.0, 0.0, 1.0),
        subtype='COLOR')
    marker_size_px: bpy.props.IntProperty(
        name="Marker size, px", default=1, min=1, max=15,
        description="Радиус маркера в пикселях")

    output_path: bpy.props.StringProperty(subtype='FILE_PATH')
    status_msg: bpy.props.StringProperty(default="")
    uuid_status_msg: bpy.props.StringProperty(default="")
    

classes = (PBA_UVE_Props,) + ops.classes + panel.classes


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.pba_uv_exporter = bpy.props.PointerProperty(
        type=PBA_UVE_Props)


def unregister():
    del bpy.types.Scene.pba_uv_exporter
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
