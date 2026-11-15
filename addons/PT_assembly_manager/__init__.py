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


def _is_our_handler(h):
    return getattr(h, "_pba_am_handler", False)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.pba_am = bpy.props.PointerProperty(type=ops.PBA_AM_Props)

    # Сначала вычистить все наши хендлеры по маркеру — это уберёт
    # накопившиеся дубликаты от предыдущих register()
    for lst in (bpy.app.handlers.depsgraph_update_post,
                bpy.app.handlers.save_pre,
                bpy.app.handlers.load_post):
        for h in list(lst):
            if _is_our_handler(h):
                lst.remove(h)

    bpy.app.handlers.depsgraph_update_post.append(ops._on_depsgraph_update)
    bpy.app.handlers.save_pre.append(ops._on_save_pre)
    bpy.app.handlers.load_post.append(ops._on_load_post)
    print("[PBA AM] handlers:",
          len(bpy.app.handlers.depsgraph_update_post),
          len(bpy.app.handlers.save_pre),
          len(bpy.app.handlers.load_post))

    if not bpy.app.timers.is_registered(ops._handlers_watchdog):
            bpy.app.timers.register(ops._handlers_watchdog,
                                    first_interval=1.0,
                                    persistent=True)


def unregister():

    if bpy.app.timers.is_registered(ops._handlers_watchdog):
        try:
            bpy.app.timers.unregister(ops._handlers_watchdog)
        except Exception:
            pass

    for lst in (bpy.app.handlers.depsgraph_update_post,
                bpy.app.handlers.save_pre,
                bpy.app.handlers.load_post):
        for h in list(lst):
            if _is_our_handler(h):
                lst.remove(h)
    del bpy.types.Scene.pba_am
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
