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


class PBA_LM_PT_main(bpy.types.Panel):
    bl_label = "PT Layout"
    bl_idname = "PT_LM_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PT Mapper"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        pba = scene.pba_lm

        # Layout
        box = layout.box()
        box.label(text="Layout", icon='IMAGE_DATA')
        box.prop(pba, "layout_path", text="")
        row = box.row(align=True)
        row.prop(pba, "plane_width_m", text="Ширина, м")
        row.prop(pba, "plane_offset_m", text="Отступ, м")
        row = box.row(align=True)
        row.operator("pba_lm.load_layout", icon='FILE_NEW')
        row.operator("pba_lm.import_json", icon='IMPORT')
        row.operator("pba_lm.frame_layout", icon='VIEW_CAMERA')

        row = layout.row(align=True)
        row.prop(pba, "mode", expand=True)
        layout.separator()
        
        if pba.mode == 'ISLAND':
            box = layout.box()
            box.label(text="Current Island", icon='MESH_GRID')
            box.label(text="Кликните на грань острова, затем 4 вершины",
          icon='INFO')
            n_v = len(pba.pending_island_corners)
            n_px = pba.pending_px_count
            box.label(text=f"Object: {pba.pending_island_obj or '—'}")
            box.label(text=f"UV layer: {pba.pending_island_uv_name or '—'}")
            box.label(text=f"Vertices: {n_v}/4,  Layout: {n_px}/4")
            box.prop(pba, "pending_part", text="Part")

            col = box.column(align=True)
            col.operator("pba_lm.start_island", icon='RESTRICT_SELECT_OFF')
            sub = col.row()
            sub.enabled = (pba.pending_island_obj != "")
            sub.operator("pba_lm.pick_island_vertices", icon='VERTEXSEL')
            col.operator("pba_lm.pick_image_points", icon='MOUSE_LMB')

            row = box.row(align=True)
            row.operator("pba_lm.commit_island", text="Commit",
                         icon='CHECKMARK')
            row.operator("pba_lm.cancel_island", text="Cancel", icon='X')

            # Snap options (используем те же, что в quad)
            col = box.column(align=True)
            col.prop(pba, "snap_to_lines")
            sub = col.row(align=True)
            sub.enabled = pba.snap_to_lines
            sub.prop(pba, "snap_radius_px")
            sub.prop(pba, "snap_threshold")
            sub2 = col.row()
            sub2.enabled = pba.snap_to_lines
            sub2.prop(pba, "snap_marker_tolerance")

            box = layout.box()
            box.label(text=f"Islands ({len(pba.islands)})",
                      icon='MESH_GRID')
            for i, isl in enumerate(pba.islands):
                row = box.row(align=True)
                active = (i == pba.active_island_index)
                icon = 'RADIOBUT_ON' if active else 'RADIOBUT_OFF'
                label = f"{isl.id}: {isl.part or '(no part)'}"
                if isl.duplicate_of:
                    label += f" ← {isl.duplicate_of}"

                op_act = row.operator("pba_lm.set_active_island",
                                       text=label, icon=icon,
                                       emboss=active)
                op_act.index = i
                
                op = row.operator("pba_lm.move_island_up", text="",
                                  icon='TRIA_UP')
                op.index = i

                op = row.operator("pba_lm.move_island_down", text="",
                                  icon='TRIA_DOWN')
                op.index = i

                op = row.operator("pba_lm.rotate_island", text="",
                                  icon='LOOP_BACK')
                op.index = i
                op.direction = -1

                op = row.operator("pba_lm.rotate_island", text="",
                                  icon='LOOP_FORWARDS')
                op.index = i
                op.direction = 1

                op = row.operator("pba_lm.flip_island", text="",
                                  icon='ARROW_LEFTRIGHT')
                op.index = i

                op = row.operator("pba_lm.duplicate_island",
                                  text="", icon='DUPLICATE')
                op.index = i

                op = row.operator("pba_lm.remove_island",
                                  text="", icon='X')
                op.index = i

            row = box.row(align=True)
            row.operator("pba_lm.sort_islands", text="Sort Islands",
                     icon='SORTALPHA')
            row.operator("pba_lm.clear_islands", text="Clear",
                         icon='TRASH')
            row.operator("pba_lm.export_json", text="Export",
                         icon='EXPORT')
        else:
            # Current quad
            box = layout.box()
            box.label(text="Current Quad", icon='MESH_GRID')
            n3d = len(pba.pending_corners_3d)
            npx = pba.pending_px_count
            box.label(text=f"3D: {n3d}/4,  Image: {npx}/4")
            box.prop(pba, "pending_part", text="Part")

            col = box.column(align=True)
            col.operator("pba_lm.capture_3d", icon='VERTEXSEL')

            row = col.row(align=True)
            op = row.operator("pba_lm.pick_image_points",
                              text="Pick 4 Points", icon='MOUSE_LMB')
            row.operator("pba_lm.undo_last_click", text="", icon='LOOP_BACK')

            row = box.row(align=True)
            sub = row.row(align=True)
            sub.enabled = (n3d == 4 and npx == 4)
            sub.operator("pba_lm.commit_quad", text="Commit",
                         icon='CHECKMARK')
            row.operator("pba_lm.cancel_quad", text="Cancel", icon='X')

            # Snap options
            col = box.column(align=True)
            col.prop(pba, "snap_to_lines")
            sub = col.row(align=True)
            sub.enabled = pba.snap_to_lines
            sub.prop(pba, "snap_radius_px")
            sub.prop(pba, "snap_threshold")
            sub2 = col.row()
            sub2.enabled = pba.snap_to_lines
            sub2.prop(pba, "snap_marker_tolerance")

            # Quads list
            box = layout.box()
            box.label(text=f"Quads ({len(pba.quads)})", icon='MESH_GRID')
            box.prop(pba, "highlight_size", text="Размер маркера, м")

            if pba.quads:
                for i, q in enumerate(pba.quads):
                    row = box.row(align=True)
                    active = (i == pba.active_quad_index)
                    icon = 'RADIOBUT_ON' if active else 'RADIOBUT_OFF'
                    label = f"{q.id}: {q.part or '(no part)'}"

                    op_act = row.operator("pba_lm.set_active_quad",
                                           text=label, icon=icon,
                                           emboss=active)
                    op_act.index = i

                    op = row.operator("pba_lm.move_quad_up", text="",
                                    icon='TRIA_UP')
                    op.index = i

                    op = row.operator("pba_lm.move_quad_down", text="",
                                    icon='TRIA_DOWN')
                    op.index = i

                    op = row.operator("pba_lm.rotate_quad", text="",
                                      icon='LOOP_BACK')
                    op.index = i
                    op.direction = -1

                    op = row.operator("pba_lm.rotate_quad", text="",
                                      icon='LOOP_FORWARDS')
                    op.index = i
                    op.direction = 1

                    op = row.operator("pba_lm.flip_quad", text="",
                                      icon='ARROW_LEFTRIGHT')
                    op.index = i

                    op = row.operator("pba_lm.remove_quad", text="", icon='X')
                    op.index = i

            row = box.row(align=True)
            row.operator("pba_lm.refresh_highlight", text="Refresh",
                         icon='FILE_REFRESH')
            row.operator("pba_lm.validate_quads", text="Validate",
                         icon='CHECKMARK')

            row = box.row(align=True)
            row.operator("pba_lm.sort_quads", text="Sort Quads",
                     icon='SORTALPHA')
            row.operator("pba_lm.clear_quads", text="Clear All", icon='TRASH')
            row.operator("pba_lm.export_json", text="Export", icon='EXPORT')

        # Status
        box = layout.box()
        box.label(text="Status", icon='INFO')
        box.label(text=pba.status_msg or "—")


classes = (PBA_LM_PT_main,)
