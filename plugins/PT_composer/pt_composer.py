#!/usr/bin/python3
# -*- coding: utf-8 -*-

import sys
import os
import json

import gi
gi.require_version('Gimp', '3.0')
gi.require_version('GimpUi', '3.0')
gi.require_version('Gtk', '3.0')
from gi.repository import Gimp, GimpUi, Gtk, GLib, GObject


class PTComposer(Gimp.PlugIn):

    def do_query_procedures(self):
        return ["python-fu-pt-save-sheet"]

    def do_set_i18n(self, name):
        return False

    def do_create_procedure(self, name):
        procedure = Gimp.ImageProcedure.new(
            self, name, Gimp.PDBProcType.PLUGIN,
            self.run_save_sheet, None)
        procedure.set_image_types("*")
        procedure.set_sensitivity_mask(
            Gimp.ProcedureSensitivityMask.DRAWABLE)
        procedure.set_menu_label("Save Sheet...")
        procedure.add_menu_path('<Image>/Filters/PT')
        procedure.set_documentation(
            "PT Save Sheet",
            "Reads layers (instance_id|piece_id), writes sheet.json "
            "and layout.quads.json",
            name)
        procedure.set_attribution("PaperTrolley", "PaperTrolley", "2026")
        return procedure

    def _state_path(self):
        try:
            gimp_dir = Gimp.directory()
        except Exception:
            gimp_dir = os.path.expanduser("~/.config/GIMP")
        return os.path.join(gimp_dir, "pba_composer_state.json")

    def _load_state(self):
        path = self._state_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self, state):
        path = self._state_path()
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[PBA] state save error: {e}")
        
    def _load_png_as_layer(self, image, png_path):
        from gi.repository import Gimp, Gio
        gfile = Gio.File.new_for_path(png_path)

        layer = None
        attempts = []

        # 1. PDB: самый стабильный путь в GIMP 3.x
        try:
            pdb = Gimp.get_pdb()
            proc = pdb.lookup_procedure('gimp-file-load-layer')
            if proc is not None:
                cfg = proc.create_config()
                try:
                    cfg.set_property('run-mode',
                                     Gimp.RunMode.NONINTERACTIVE)
                except Exception:
                    try:
                        cfg.set_property('run-mode', 1)
                    except Exception:
                        pass
                cfg.set_property('image', image)
                cfg.set_property('file', gfile)
                r = proc.run(cfg)
                try:
                    layer = r.index(1)
                except Exception:
                    try:
                        layer = r.get_layer(0)
                    except Exception:
                        layer = None
                if layer is None:
                    attempts.append("PDB: результат не Layer")
            else:
                attempts.append("PDB: процедура не найдена")
        except Exception as e:
            attempts.append(f"PDB: {e}")

        # 2. Прямой вызов с int run_mode
        if layer is None:
            try:
                layer = Gimp.file_load_layer(image, 1, gfile)
            except Exception as e:
                attempts.append(f"int(1): {e}")

        # 3. Прямой вызов со строкой
        if layer is None:
            try:
                layer = Gimp.file_load_layer(
                    image, "noninteractive", gfile)
            except Exception as e:
                attempts.append(f"str: {e}")

        # 4. image.file_load_layer
        if layer is None:
            try:
                layer = image.file_load_layer(
                    Gimp.RunMode.NONINTERACTIVE, gfile)
            except Exception as e:
                attempts.append(f"image.file_load_layer: {e}")

        # 5. image.file_load_layer с int
        if layer is None:
            try:
                layer = image.file_load_layer(1, gfile)
            except Exception as e:
                attempts.append(f"image.file_load_layer(1): {e}")

        if layer is None:
            return None, " | ".join(attempts)

        # Вставить в image
        try:
            if image.insert_layer(layer, None, -1):
                return layer, None
            return None, "insert_layer вернул False"
        except Exception as e:
            return None, f"insert_layer: {e}"

    def run_save_sheet(self, procedure, run_mode, image, drawables,
                        config, data):
        try:
            params = self._ask_paths(image)
            if params is None:
                return procedure.new_return_values(
                    Gimp.PDBStatusType.CANCEL, GLib.Error())

            (assembly_path, piece_id, scale, dpi, variant, mode) = params

            if mode == "load":
                result = self._do_auto_load(
                    image, assembly_path, piece_id, scale, dpi)
            else:
                result = self._do_save(
                    image, assembly_path, piece_id, variant, scale, dpi)

            if result.get("error"):
                Gimp.message("ОШИБКА:\n" + result["error"])
                return procedure.new_return_values(
                    Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error())

            Gimp.message(result["summary"])
            return procedure.new_return_values(
                Gimp.PDBStatusType.SUCCESS, GLib.Error())
        except Exception as e:
            import traceback
            traceback.print_exc()
            Gimp.message("ОШИБКА:\n" + str(e))
            return procedure.new_return_values(
                Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error())

    def _ask_paths(self, image):
        state = self._load_state()

        dialog = Gtk.Dialog(title="PBA Composer", modal=True)
        dialog.add_buttons(
            "Отмена", Gtk.ResponseType.CANCEL,
            "Загрузить pieces", 1,
            "Сохранить sheet.json", 2)
        dialog.set_default_size(600, 280)

        content = dialog.get_content_area()
        grid = Gtk.Grid(margin=12, row_spacing=10, column_spacing=10)
        content.pack_start(grid, True, True, 0)

        # assembly.json
        grid.attach(Gtk.Label(label="assembly.json:", xalign=0), 0, 0, 1, 1)
        asm_btn = Gtk.FileChooserButton.new(
            "Выберите assembly.json", Gtk.FileChooserAction.OPEN)
        f = Gtk.FileFilter()
        f.set_name("JSON")
        f.add_pattern("*.assembly.json")
        f.add_pattern("*.json")
        asm_btn.add_filter(f)

        last_asm = state.get("assembly_path", "")
        if last_asm and os.path.exists(last_asm):
            try:
                from gi.repository import Gio
                asm_btn.set_file(Gio.File.new_for_path(last_asm))
            except Exception:
                pass
        grid.attach(asm_btn, 1, 0, 1, 1)

        # piece_id
        grid.attach(Gtk.Label(label="piece_id:", xalign=0), 0, 1, 1, 1)
        piece_entry = Gtk.Entry()
        piece_entry.set_text(state.get("piece_id", "standard_87-600"))
        grid.attach(piece_entry, 1, 1, 1, 1)

        # scale + dpi
        grid.attach(Gtk.Label(label="scale (1:N):", xalign=0), 0, 2, 1, 1)
        scale_entry = Gtk.Entry()
        scale_entry.set_text(str(state.get("scale", 87)))
        grid.attach(scale_entry, 1, 2, 1, 1)

        grid.attach(Gtk.Label(label="dpi:", xalign=0), 0, 3, 1, 1)
        dpi_entry = Gtk.Entry()
        dpi_entry.set_text(str(state.get("dpi", 600)))
        grid.attach(dpi_entry, 1, 3, 1, 1)

        # Автозаполнение при изменении piece_id
        import re
        def on_piece_changed(_entry):
            name = piece_entry.get_text().strip()
            m = re.search(r'(\d+)[_-](\d+)\s*(?:dpi)?$', name.lower())
            if m:
                scale_entry.set_text(m.group(1))
                dpi_entry.set_text(m.group(2))
        piece_entry.connect("changed", on_piece_changed)

        info = Gtk.Label(
            label="Загрузить pieces: подгружает PNG всех instances\n"
                  "Сохранить sheet.json: сохраняет раскладку\n"
                  "Output: <assembly_dir>/sheets/<piece_id>/",
            xalign=0)
        grid.attach(info, 0, 4, 2, 1)

        dialog.show_all()
        response = dialog.run()
        assembly_path = asm_btn.get_filename()
        piece_id = piece_entry.get_text().strip()
        scale_text = scale_entry.get_text().strip()
        dpi_text = dpi_entry.get_text().strip()
        dialog.destroy()

        if response not in (1, 2):
            return None
        if not assembly_path or not piece_id:
            return None
        try:
            scale = int(scale_text)
            dpi = int(dpi_text)
        except ValueError:
            return None
        if scale <= 0 or dpi <= 0:
            return None

        # Сохраняем состояние для следующего вызова
        self._save_state({
            "assembly_path": assembly_path,
            "piece_id": piece_id,
            "scale": scale,
            "dpi": dpi,
        })

        mode = "load" if response == 1 else "save"
        variant = f"1-{scale}_{dpi}dpi"
        return (assembly_path, piece_id, scale, dpi, variant, mode)

    def _do_auto_load(self, image, assembly_path, piece_id, scale, dpi):
        try:
            with open(assembly_path, 'r', encoding='utf-8') as f:
                assembly = json.load(f)
        except Exception as e:
            return {"error": f"assembly.json: {e}", "summary": ""}

        existing_layer_names = {l.get_name() for l in image.get_layers()}
        loaded = 0
        skipped_existing = 0
        skipped_missing = 0
        skipped_scale = 0
        warnings = []

        instances = assembly.get("components", [])
        for idx, inst in enumerate(instances):
            iid = inst.get("instance_id", "")
            comp_path = inst.get("component_path", "")
            if not iid or not comp_path:
                continue

            png_path = os.path.join(
                comp_path, "pieces", piece_id + ".png")
            json_path = os.path.join(
                comp_path, "pieces", piece_id + ".json")

            if not os.path.exists(png_path):
                warnings.append(
                    f"{iid}: нет {piece_id}.png")
                skipped_missing += 1
                continue

            if os.path.exists(json_path):
                try:
                    with open(json_path, 'r', encoding='utf-8') as f:
                        pdata = json.load(f)
                    ps = pdata.get("scale_den")
                    pd = pdata.get("dpi")
                    if ps and int(ps) != scale:
                        warnings.append(
                            f"{iid}: piece scale 1:{ps} != "
                            f"1:{scale}")
                        skipped_scale += 1
                        continue
                    if pd and int(pd) != dpi:
                        warnings.append(
                            f"{iid}: piece dpi {pd} != {dpi}")
                        skipped_scale += 1
                        continue
                except Exception as e:
                    warnings.append(f"{iid}: piece.json: {e}")

            layer_name = f"{iid}|{piece_id}"
            if layer_name in existing_layer_names:
                skipped_existing += 1
                continue

            try:
                layer, err = self._load_png_as_layer(image, png_path)
                if err:
                    warnings.append(f"{iid}: {err}")
                    continue

                layer.set_name(layer_name)
                pos_x = (idx % 4) * 150
                pos_y = (idx // 4) * 150
                try:
                    layer.set_offsets((pos_x, pos_y))
                except (AttributeError, TypeError):
                    try:
                        layer.set_offsets(pos_x, pos_y)
                    except Exception:
                        pass

                existing_layer_names.add(layer_name)
                loaded += 1
            except Exception as e:
                import traceback
                traceback.print_exc()
                warnings.append(f"{iid}: {e}")

        summary = (f"Loaded: {loaded} layers\n"
                   f"Skipped (already loaded): {skipped_existing}\n"
                   f"Skipped (missing piece): {skipped_missing}\n"
                   f"Skipped (scale/dpi mismatch): {skipped_scale}")
        if warnings:
            summary += f"\n\nWarnings ({len(warnings)}):\n"
            summary += "\n".join(warnings[:12])
            if len(warnings) > 12:
                summary += f"\n... и ещё {len(warnings)-12}"

        return {"error": None, "summary": summary}

    def _do_save(self, image, assembly_path, piece_id, variant, scale, dpi):
        try:
            with open(assembly_path, 'r', encoding='utf-8') as f:
                assembly = json.load(f)
        except Exception as e:
            return {"error": f"assembly.json: {e}", "summary": ""}

        # Out dir — рядом с assembly, подпапка по piece_id
        out_dir = os.path.join(
            os.path.dirname(assembly_path), assembly_path.replace(".assembly.json", "") + "_sheets", piece_id)

        # instance_id → component_id
        inst_to_comp = {}
        for inst in assembly.get("components", []):
            iid = inst.get("instance_id", "")
            cid = inst.get("component_id", "")
            if iid and cid:
                inst_to_comp[iid] = cid

        # (component_id, piece_id) → piece_data
        pieces_index = {}
        for inst in assembly.get("components", []):
            comp_path = inst.get("component_path", "")
            cid = inst.get("component_id", "")
            if not comp_path or not cid:
                continue
            pieces_dir = os.path.join(comp_path, "pieces")
            if not os.path.isdir(pieces_dir):
                continue
            for root, _dirs, files in os.walk(pieces_dir):
                for fname in files:
                    if not fname.endswith(".json"):
                        continue
                    jpath = os.path.join(root, fname)
                    try:
                        with open(jpath, 'r', encoding='utf-8') as fp:
                            pdata = json.load(fp)
                    except Exception:
                        continue
                    pid = pdata.get("piece_id")
                    if pid and (cid, pid) not in pieces_index:
                        pieces_index[(cid, pid)] = pdata

        SKIP_LAYER_NAMES = {"fon", "background", "фон", "bg"}

        placements = []
        warnings = []
        W = image.get_width()
        H = image.get_height()

        for layer in image.get_layers():
            if not layer.get_visible():
                continue
            name = layer.get_name().strip()
            if name.lower() in SKIP_LAYER_NAMES:
                continue
            if '|' not in name:
                warnings.append(f"Слой '{name}': нет '|', пропуск")
                continue
            inst_id, piece_id = name.split('|', 1)
            inst_id = inst_id.strip()
            piece_id = piece_id.strip()

            if not inst_id:
                warnings.append(f"Слой '{name}': пустой instance_id")
                continue

            comp_id = inst_to_comp.get(inst_id)
            if comp_id is None:
                warnings.append(
                    f"Слой '{name}': instance '{inst_id}' "
                    f"отсутствует в assembly.json")
                continue

            key = (comp_id, piece_id)
            if key not in pieces_index:
                warnings.append(
                    f"Слой '{name}': piece '{piece_id}' не найден "
                    f"у компонента '{comp_id}'")
                continue

            offs = layer.get_offsets()
            try:
                pos_x = int(offs.offset_x)
                pos_y = int(offs.offset_y)
            except AttributeError:
                if isinstance(offs, tuple):
                    if len(offs) == 3:
                        _, pos_x, pos_y = offs
                    else:
                        pos_x, pos_y = offs
                else:
                    warnings.append(
                        f"Слой '{name}': не удалось прочитать позицию")
                    continue

            placement_id = f"{inst_id}__{piece_id}"
            placements.append({
                "placement_id": placement_id,
                "instance_id": inst_id,
                "component_id": comp_id,
                "piece_id": piece_id,
                "position_px": [pos_x, pos_y],
            })

        # Проверка выхода за границы листа
        for p in placements:
            key = (p["component_id"], p["piece_id"])
            pdata = pieces_index[key]
            px, py = p["position_px"]
            piece_size = pdata.get("size_px", [0, 0])
            if px + piece_size[0] > W or py + piece_size[1] > H:
                warnings.append(
                    f"'{p['placement_id']}' выходит за лист: "
                    f"pos=({px},{py}) + size={piece_size} > "
                    f"page=({W},{H}).")

        # Проверка пропущенных instances
        used_instance_ids = {p["instance_id"] for p in placements}
        for inst in assembly.get("components", []):
            iid = inst.get("instance_id", "")
            if iid and iid not in used_instance_ids:
                warnings.append(
                    f"Instance '{iid}' не размещён на листе")

        os.makedirs(out_dir, exist_ok=True)

        sheet_data = {
            "sheet_id": f"{assembly.get('assembly_id')}_{variant}",
            "variant": variant,
            "scale_den": scale,
            "dpi": dpi,
            "page": {"width_px": W, "height_px": H},
            "placements": placements,
        }
        with open(os.path.join(out_dir, "sheet.json"), 'w',
                    encoding='utf-8') as f:
            json.dump(sheet_data, f, ensure_ascii=False, indent=2)

        islands_out = []
        for p in placements:
            key = (p["component_id"], p["piece_id"])
            pdata = pieces_index[key]
            px, py = p["position_px"]
            uv_map = pdata.get("uv_map", "")
            for isl in pdata.get("islands", []):
                corners_layout = [
                    [px + int(round(cx)), py + int(round(cy))]
                    for cx, cy in isl.get("corners_piece_px", [])
                ]
                corners_uv = isl.get("corners_uv", [])
                if len(corners_layout) != 4 or len(corners_uv) != 4:
                    warnings.append(
                        f"Piece '{p['piece_id']}' / "
                        f"island '{isl.get('island_id')}': не 4 угла")
                    continue
                islands_out.append({
                    "placement_id": p["placement_id"],
                    "piece_id": p["piece_id"],
                    "island_id": isl.get("island_id", ""),
                    "instance_id": p["instance_id"],
                    "obj_name": isl.get("obj_name", ""),
                    "uv_map": uv_map,
                    "corners_uv": corners_uv,
                    "corners_layout_px": corners_layout,
                    "vertices": isl.get("vertices", []),
                })

        layout_data = {
            "sheet_id": sheet_data["sheet_id"],
            "page": {"width_px": W, "height_px": H},
            "islands": islands_out,
            "quads": [],
        }
        
        with open(os.path.join(out_dir, "layout.quads.json"), 'w',
                    encoding='utf-8') as f:
            json.dump(layout_data, f, ensure_ascii=False, indent=2)

        summary = (f"Placements: {len(placements)}\n"
                   f"Islands: {len(islands_out)}\n"
                   f"Output: {out_dir}")
        if warnings:
            summary += f"\n\nWarnings ({len(warnings)}):\n"
            summary += "\n".join(warnings[:12])
            if len(warnings) > 12:
                summary += f"\n... и ещё {len(warnings)-12}"
        return {"error": None, "summary": summary}


Gimp.main(PTComposer.__gtype__, sys.argv)
