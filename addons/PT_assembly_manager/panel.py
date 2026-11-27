import bpy
import os
from .ops import _get_inst_mtime


class PBA_AM_PT_main(bpy.types.Panel):
    bl_label = "PT Assembly Manager"
    bl_idname = "PT_AM_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PT Assembly"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        pba = scene.pba_am

        # Создание компонента
        box = layout.box()
        box.label(text="Create Component", icon='MESH_DATA')
        box.prop(pba, "component_id")
        box.prop(pba, "components_dir", text="Dir")
        box.operator("pba_am.create_component", icon='FILE_NEW')
        box.label(text="(выделите объекты → нажмите)", icon='INFO')

        # Сборка
        box = layout.box()
        box.label(text="Assembly", icon='FILE_BLEND')

        blend_path = bpy.data.filepath
        if blend_path:
            box.label(text=os.path.basename(blend_path),
                        icon='CHECKMARK')
            base = os.path.splitext(os.path.basename(blend_path))[0]
            box.label(text=f"{base}.assembly.json", icon='FILE')
        else:
            box.label(text=".blend не сохранён", icon='ERROR')
            box.label(text="File → Save As...", icon='INFO')

        row = box.row(align=True)
        row.enabled = bool(blend_path)
        row.operator("pba_am.load_assembly", text="Load",
                        icon='FILE_FOLDER')
        row.operator("pba_am.save_assembly", text="Save",
                        icon='FILE_TICK')

        # Импорт
        box = layout.box()
        box.label(text="Import Component", icon='IMPORT')
        box.operator("pba_am.import_component",
                        text="Import mesh.blend...", icon='ADD')

        # Список экземпляров
        box = layout.box()
        box.label(text=f"Instances ({len(pba.instances)})",
                    icon='OUTLINER_COLLECTION')
        if not pba.instances:
            box.label(text="Нет компонентов", icon='INFO')
        else:
            for i, inst in enumerate(pba.instances):
                active = (i == pba.active_instance_index)
                icon = 'RADIOBUT_ON' if active else 'RADIOBUT_OFF'
                row = box.row(align=True)
                op_sel = row.operator(
                    "pba_am.select_instance",
                    text=inst.instance_id, icon=icon, emboss=active)
                op_sel.index = i
                row.label(text=f"[{inst.component_id}]")
                # Индикатор устаревания
                stale = False
                inst_mtime = _get_inst_mtime(inst)
                if inst_mtime > 0 and inst.component_path:
                    mp = os.path.join(inst.component_path, "mesh.blend")
                    if os.path.exists(mp):
                        try:
                            if os.path.getmtime(mp) > inst_mtime + 1.0:
                                stale = True
                        except Exception:
                            pass
                if stale:
                    row.label(text="", icon='ERROR')
                if stale:
                    row.label(text="", icon='ERROR')
                if inst.has_local_changes:
                    row.label(text="", icon='UNLINKED')
                op = row.operator("pba_am.publish_instance",
                                    text="", icon='IMPORT')
                op.index = i
                op = row.operator("pba_am.reload_component",
                                    text="", icon='FILE_REFRESH')
                op.index = i
                op = row.operator("pba_am.remove_instance",
                                    text="", icon='X')
                op.index = i

        # Валидация
        box = layout.box()
        row = box.row(align=True)
        row.operator("pba_am.reload_all", text="Reload All",
                     icon='FILE_REFRESH')
        row.operator("pba_am.validate", text="Validate",
                     icon='CHECKMARK')

        # Статус
        box = layout.box()
        box.label(text="Status", icon='INFO')
        for line in (pba.status_msg or "—").split("\n")[-3:]:
            box.label(text=line)


classes = (PBA_AM_PT_main,)
