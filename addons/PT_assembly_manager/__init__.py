bl_info = {
    "name": "PT Assembly Manager",
    "author": "PaperTrolley",
    "version": (0, 4, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > PT Assembly Manager",
    "description": "Создание компонентов и сборка моделей",
    "category": "Modeling",
}

import bpy
from . import ops, panel

classes = ops.classes + panel.classes


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.pba_am = bpy.props.PointerProperty(type=ops.PBA_AM_Props)


def unregister():
    del bpy.types.Scene.pba_am
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
