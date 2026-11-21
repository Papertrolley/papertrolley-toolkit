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
import bpy


class PBA_TRI_PT_main(bpy.types.Panel):
    bl_label = "PT Triangulation"
    bl_idname = "PT_TRI_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PT Triangulation"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        p = scene.pba_tri

        # Slots
        box = layout.box()
        box.label(text=f"Photos ({len(p.photos)}/3)", icon='IMAGE_DATA')

        for i in range(3):
            if i >= len(p.photos):
                row = box.row()
                row.label(text=f"Слот {i + 1}: не инициализирован",
                          icon='ERROR')
                continue

            slot = p.photos[i]
            active = (i == p.active_slot)

            sub = box.box()
            row = sub.row(align=True)
            icon = 'RADIOBUT_ON' if active else 'RADIOBUT_OFF'
            op = row.operator("pba_tri.activate_slot",
                              text=f"Слот {i + 1}", icon=icon,
                              emboss=False)
            op.slot_index = i

            row = sub.row(align=True)
            row.prop(slot, "json_path", text="")

            row = sub.row(align=True)
            op = row.operator("pba_tri.load_slot", text="Load",
                              icon='FILE_NEW')
            op.slot_index = i
            op = row.operator("pba_tri.activate_slot", text="Activate",
                              icon='CAMERA_DATA')
            op.slot_index = i

            if slot.loaded:
                # Разбивка пар по instance — полезно при сборках
                # с несколькими копиями одного компонента
                by_iid = {}
                for c in slot.correspondences:
                    by_iid[c.instance_id] = by_iid.get(c.instance_id, 0) + 1
                iid_parts = ", ".join(
                    f"{iid or '<local>'}:{n}"
                    for iid, n in sorted(by_iid.items()))
                row = sub.row()
                row.label(
                    text=f"{slot.width_px}×{slot.height_px}, "
                         f"{len(slot.correspondences)} пар")
                row = sub.row()
                row.label(text=iid_parts, icon='OUTLINER_COLLECTION')

        layout.separator()
        layout.operator("pba_tri.auto_triangulate",
                        icon='AUTOMERGE_ON')

        # Manual point
        box = layout.box()
        box.label(text="Manual Point", icon='ADD')

        if not p.building:
            box.operator("pba_tri.start_new_point",
                         text="Start New Point", icon='ADD')
        else:
            # Показать текущее состояние слотов
            row = box.row(align=True)
            for i in range(3):
                slot = p.photos[i]
                if not slot.loaded:
                    icon = 'REMOVE'
                else:
                    clicked = any(cl.slot == i for cl in p.clicks)
                    icon = 'CHECKMARK' if clicked else 'X'
                row.label(text="", icon=icon)

            row = box.row(align=True)
            row.operator("pba_tri.pick_click", text="Pick Here",
                         icon='MOUSE_LMB')
            row = box.row(align=True)
            row.operator("pba_tri.commit_point", text="Add Vertex",
                         icon='CHECKMARK')
            row.operator("pba_tri.cancel_point", text="Cancel",
                         icon='X')

        # Output
        box = layout.box()
        box.label(text="Output", icon='MESH_DATA')
        box.prop(p, "point_size", text="Размер маркера, м")
        row = box.row(align=True)
        row.operator("pba_tri.open_output", text="Select",
                     icon='RESTRICT_SELECT_OFF')
        row.operator("pba_tri.clear_output", text="Clear",
                     icon='TRASH')

        # Status
        box = layout.box()
        box.label(text="Status", icon='INFO')
        box.label(text=p.status_msg or "—")


classes = (PBA_TRI_PT_main,)
