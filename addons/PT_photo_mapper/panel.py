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


class PBA_VM_PT_main(bpy.types.Panel):
    bl_label = "PT Photo"
    bl_idname = "PT_PM_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PT Mapper"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        pba = scene.pba_vm

        box = layout.box()
        box.label(text="Photo", icon='IMAGE_DATA')
        box.prop(pba, "photo_path", text="")
        box.prop(pba, "restore_from_json", text="Restore from JSON")

        box = layout.box()
        box.label(text="Camera Start", icon='CAMERA_DATA')
        box.prop(pba, "camera_position", text="")
        box.prop(pba, "crop_factor")
        box = layout.box()
        box.label(text="Manual Camera Override", icon='PREFERENCES')
        box.prop(pba, "use_manual_camera")
        sub = box.column(align=True)
        sub.enabled = pba.use_manual_camera
        row = sub.row(align=True)
        row.prop(pba, "manual_native_width", text="Native W")
        row.prop(pba, "manual_native_height", text="×")
        sub.prop(pba, "manual_sensor_width")
        sub.prop(pba, "manual_focal_length")
        if pba.use_manual_camera:
            hint = sub.row()
            hint.label(text="0 = авто (из EXIF)", icon='INFO')
        box = layout.box()
        box.label(text="Crop Offset", icon='FULLSCREEN_EXIT')
        row = box.row(align=True)
        row.prop(pba, "crop_offset_x", text="X")
        row.prop(pba, "crop_offset_y", text="Y")
        hint = box.row()
        hint.label(text="Подбор: начни с 0, смотри на визуальный "
                        "сдвиг модели", icon='INFO')

        col = layout.column(align=True)
        col.operator("pba_vm.load_photo", icon='CAMERA_DATA')

        op = col.operator("pba_vm.save_vertices", icon='EXPORT')
        op.check_visibility = pba.visibility_check

        box = layout.box()
        box.label(text="Visibility", icon='HIDE_OFF')
        box.prop(pba, "visibility_check")

        # Correspondences
        box = layout.box()
        box.label(text=f"Correspondences ({len(pba.correspondences)})",
                  icon='TRACKER')

        row = box.row(align=True)
        row.label(text="3D: " + ("✓" if pba.pending_3d_set else "—"))
        row.label(text="2D: " + ("✓" if pba.pending_2d_set else "—"))

        col = box.column(align=True)
        col.operator("pba_vm.pick_3d", icon='VERTEXSEL')
        col.operator("pba_vm.pick_2d", icon='MOUSE_LMB')
        
        if pba.correspondences:
            box.separator()
            active = sum(1 for c in pba.correspondences if c.enabled)
            total = len(pba.correspondences)
            box.label(text=f"Активных: {active} / {total}")

            for i, c in enumerate(pba.correspondences):
                row = box.row(align=True)
                if not c.enabled:
                    # Отключённая пара — серая
                    icon = 'HIDE_ON'
                elif c.last_error < 0:
                    icon = 'RADIOBUT_OFF'
                elif c.last_error < 5:
                    icon = 'CHECKMARK'
                elif c.last_error < 20:
                    icon = 'INFO'
                else:
                    icon = 'ERROR'

                op_sel = row.operator("pba_vm.select_correspondence",
                                   text="", icon='ZOOM_SELECTED')
                op_sel.index = i

                #op_snap = row.operator("pba_vm.snap_to_click", text="", icon='SNAP_VERTEX')
                #op_snap.index = i

                label = f"{i+1}. {c.obj_name} v{c.vertex_index} → " \
                        f"({c.x2d[0]:.0f}, {c.x2d[1]:.0f})"
                row.label(text=label, icon=icon)
                if c.enabled and c.last_error >= 0:
                    row.label(text=f"{c.last_error:.1f}px")
                else:
                    row.label(text="—")
                op = row.operator("pba_vm.remove_pair", text="", icon='X')
                op.index = i

        # Управление
        row = box.row(align=True)
        row.operator("pba_vm.solve_camera", text="Solve", icon='CAMERA_DATA')
        row.operator("pba_vm.diagnose_pairs", text="Diagnose", icon='VIEWZOOM')
        row.operator("pba_vm.remove_outliers", text="Outliers", icon='FILTER')

        row = box.row(align=True)
        box.prop(pba, "use_distortion_k1")
        box.prop(pba, "use_distortion_k2")
        box.prop(pba, "huber_delta")

        row = box.row(align=True)
        row.operator("pba_vm.disable_selected", text="Disable Sel.",
                     icon='HIDE_ON')
        row.operator("pba_vm.enable_selected", text="Enable Sel.",
                     icon='HIDE_OFF')
        row.operator("pba_vm.sort_pairs", text="Sort Pairs",
                 icon='SORTALPHA')

        box.operator("pba_vm.clear_pairs", text="Clear All", icon='TRASH')
        box.operator("pba_vm.enable_all", text="Enable All", icon='RESTRICT_VIEW_OFF')

        # Nudge
        box = layout.box()
        box.label(text="Nudge (Edit Mode)", icon='ARROW_LEFTRIGHT')
        box.prop(pba, "nudge_step", text="Шаг, м")

        row = box.row(align=True)
        op = row.operator("pba_vm.nudge_vertex", text="−X")
        op.axis = 'X'
        op.direction = -1
        op = row.operator("pba_vm.nudge_vertex", text="+X")
        op.axis = 'X'
        op.direction = 1

        row = box.row(align=True)
        op = row.operator("pba_vm.nudge_vertex", text="−Y")
        op.axis = 'Y'
        op.direction = -1
        op = row.operator("pba_vm.nudge_vertex", text="+Y")
        op.axis = 'Y'
        op.direction = 1

        row = box.row(align=True)
        op = row.operator("pba_vm.nudge_vertex", text="−Z")
        op.axis = 'Z'
        op.direction = -1
        op = row.operator("pba_vm.nudge_vertex", text="+Z")
        op.axis = 'Z'
        op.direction = 1

        box = layout.box()
        box.label(text="Status", icon='INFO')
        box.label(text=pba.status_msg or "—")


classes = (PBA_VM_PT_main,)
