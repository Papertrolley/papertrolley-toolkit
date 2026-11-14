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
#!/usr/bin/python3
# -*- coding: utf-8 -*-

import sys
import math
import gi
from collections import deque

gi.require_version('Gimp', '3.0')
gi.require_version('GimpUi', '3.0')
gi.require_version('Gegl', '0.4')
from gi.repository import Gimp, GimpUi, GLib, Gegl, GObject


def color_to_rgba_u8(color):
    """Gegl.Color -> bytes(4) в порядке R, G, B, A (0-255)."""
    r, g, b, a = color.get_rgba()
    return bytes([
        max(0, min(255, int(r * 255 + 0.5))),
        max(0, min(255, int(g * 255 + 0.5))),
        max(0, min(255, int(b * 255 + 0.5))),
        max(0, min(255, int(a * 255 + 0.5))),
    ])


def compute_adaptive_angle(prev_v, cur_v, next_v, base_deg):
    v1 = (prev_v[0] - cur_v[0], prev_v[1] - cur_v[1])
    v2 = (next_v[0] - cur_v[0], next_v[1] - cur_v[1])
    l1, l2 = math.hypot(*v1), math.hypot(*v2)
    if l1 < 1e-6 or l2 < 1e-6:
        return base_deg
    dot = max(-1.0, min(1.0,
              (v1[0]*v2[0] + v1[1]*v2[1]) / (l1 * l2)))
    theta = math.degrees(math.acos(dot))
    if 2 * base_deg > theta:   # θ мало́ — стороны налезут друг на друга
        return theta / 2
    return base_deg


def draw_flap_on_layer(source_layer, draw_layer, p1, p2, height,
                       angle_start_deg, angle_end_deg,
                       fill_color, base_color, base_thickness, outline_color, outline_thickness,
                       outward_sign):
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    L = math.hypot(dx, dy)
    if L < 0.5 or height <= 0:
        return

    ux, uy = dx / L, dy / L
    nx, ny = outward_sign * (-uy), outward_sign * ux

    a_s = math.radians(max(1.0, min(89.0, angle_start_deg)))
    a_e = math.radians(max(1.0, min(89.0, angle_end_deg)))
    sin_sum = math.sin(a_s + a_e)
    h_meet = (L * math.sin(a_s) * math.sin(a_e) / sin_sum
              if sin_sum > 1e-9 else float('inf'))
    h_eff = min(height, h_meet)

    inset_s = h_eff / math.tan(a_s) if a_s > 1e-6 else 0.0
    inset_e = h_eff / math.tan(a_e) if a_e > 1e-6 else 0.0

    corners = [
        p1, p2,
        (p2[0] + nx*h_eff - ux*inset_e, p2[1] + ny*h_eff - uy*inset_e),
        (p1[0] + nx*h_eff + ux*inset_s, p1[1] + ny*h_eff + uy*inset_s),
    ]
    pad = int(math.ceil(outline_thickness)) + 2
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    min_x = max(0, int(math.floor(min(xs))) - pad)
    max_x = min(draw_layer.get_width(), int(math.ceil(max(xs))) + pad)
    min_y = max(0, int(math.floor(min(ys))) - pad)
    max_y = min(draw_layer.get_height(), int(math.ceil(max(ys))) + pad)
    bw, bh = max_x - min_x, max_y - min_y
    if bw <= 0 or bh <= 0:
        return

    rect = Gegl.Rectangle.new(min_x, min_y, bw, bh)

    # --- ЧТЕНИЕ источника (активного слоя) ---
    src_buf = source_layer.get_buffer()
    src_data = bytes(src_buf.get(rect, 1.0, "RGBA u8", Gegl.AbyssPolicy.NONE))
    src_alpha = bytearray(src_data[3::4])

    # --- БУФЕР отрисовки (временный слой) ---
    dst_buf = draw_layer.get_buffer()
    pixel_data = bytearray(bytes(dst_buf.get(rect, 1.0, "RGBA u8", Gegl.AbyssPolicy.NONE)))

    def to_local(px, py):
        rx, ry = px + 0.5 - p1[0], py + 0.5 - p1[1]
        return rx*ux + ry*uy, rx*nx + ry*ny

    def in_trap(px, py):
        s, t = to_local(px, py)
        if t < 0.0 or t > h_eff + 0.5:
            return False
        tc = min(h_eff, t)
        fr = tc / h_eff if h_eff > 1e-6 else 0.0
        return (inset_s * fr - 0.5) <= s <= (L - inset_e * fr + 0.5)

    def src_alpha_at(px, py):
        if px < min_x or py < min_y or px >= max_x or py >= max_y:
            return 0
        return src_alpha[(py - min_y) * bw + (px - min_x)]

    def is_passable(px, py):
        return (px >= min_x and py >= min_y and px < max_x and py < max_y
                and in_trap(px, py) and src_alpha_at(px, py) == 0)

    fill_rgba    = color_to_rgba_u8(fill_color)
    base_rgba = color_to_rgba_u8(base_color)
    outline_rgba = color_to_rgba_u8(outline_color)
    # 0 в UI означает "как у обводки" — сохраняем обратную совместимость
    effective_base_thickness = (base_thickness if base_thickness > 0
                                else outline_thickness)

    def set_pixel(px, py, rgba):
        idx = ((py - min_y) * bw + (px - min_x)) * 4
        pixel_data[idx:idx + 4] = rgba

    dirs8  = [(-1,-1),(0,-1),(1,-1),(-1,0),(1,0),(-1,1),(0,1),(1,1)]
    away3  = sorted(dirs8, key=lambda d: -(d[0]*nx + d[1]*ny))[:3]

    # === PASS 1: BFS от основания ===
    body = set()
    queue = deque()
    for y in range(min_y, max_y):
        for x in range(min_x, max_x):
            if not is_passable(x, y):
                continue
            _, t = to_local(x, y)
            if t <= 1.5:                       # полоса основания ~1 пиксель
                queue.append((x, y))
                body.add((x, y))
    if not body:
        return

    while queue:
        x, y = queue.popleft()
        for ddx, ddy in away3:
            px, py = x + ddx, y + ddy
            if (px, py) in body or not is_passable(px, py):
                continue
            if ddx != 0 and ddy != 0:          # no corner cutting
                if not (is_passable(x + ddx, y) or is_passable(x, y + ddy)):
                    continue
            body.add((px, py))
            queue.append((px, py))

    # --- Pass 1a: заливка тела ---
    for (x, y) in body:
        set_pixel(x, y, fill_rgba)

    # === PASS 2: обводка ===
    if outline_thickness > 0:
        max_d = int(math.ceil(outline_thickness))
        offsets = [(ddx, ddy)
                   for ddx in range(-max_d, max_d + 1)
                   for ddy in range(-max_d, max_d + 1)
                   if (ddx, ddy) != (0, 0)
                   and max(abs(ddx), abs(ddy)) <= outline_thickness]
        outline_pixels = []
        for (x, y) in body:
            for (ddx, ddy) in offsets:
                if (x + ddx, y + ddy) not in body:
                    outline_pixels.append((x, y))
                    break
        for (x, y) in outline_pixels:
            set_pixel(x, y, outline_rgba)

    # === PASS 3: перекраска основания поверх обводки ===
    if effective_base_thickness > 0:
        for (x, y) in body:
            _, t = to_local(x, y)
            if t < effective_base_thickness:
                set_pixel(x, y, base_rgba)

    dst_buf.set(rect, "RGBA u8", bytes(pixel_data))
    dst_buf.flush()
    draw_layer.update(rect.x, rect.y, rect.width, rect.height)
    

def get_path_vertices(active_path):
    """
    Извлекает список вершин (углов) из активного контура изображения.
    
    Возвращает список кортежей [(x1, y1), (x2, y2), ...] или пустой список,
    если активный контур не найден или не содержит вершин.
    """
    if not active_path:
        Gimp.message("Пожалуйста, выберите активный контур (Path).")
        return []

    # Получаем список "штрихов" (strokes) в контуре. 
    # Для простой ломаной линии обычно достаточно одного штриха.
    strokes = active_path.get_strokes()
    if not strokes:
        Gimp.message("Контур не содержит штрихов.")
        return []

    # Берем первый штрих. Для сложных фигур логику можно расширить.
    stroke_id = strokes[0]

    # Получаем "сырые" точки штриха.
    # Объект points содержит список controlpoints.
    points_data = active_path.stroke_get_points(stroke_id)
    controlpoints = points_data.controlpoints

    # --- Разбор controlpoints ---
    # Controlpoints — это плоский список чисел с плавающей точкой.
    # Для каждой точки на контуре здесь хранится 6 значений (x, y для самой точки 
    # и для двух её контрольных точек Безье). 
    # Нам нужны только координаты самих вершин, которые идут первыми в каждой группе из 6 чисел.
    # Пример: [x0, y0, cx0, cy0, cx1, cy1, x1, y1, cx2, cy2, ...]
    # где (x0, y0) — первая вершина, (x1, y1) — вторая и т.д.
    
    if not controlpoints or len(controlpoints) % 6 != 0:
        Gimp.message("Не удалось корректно разобрать точки контура.")
        return []

    vertices = []
    # Проходим по списку с шагом 6, извлекая координаты вершин.
    for i in range(0, len(controlpoints), 6):
        x = controlpoints[i]
        y = controlpoints[i+1]
        vertices.append((x, y))

    return vertices

# --- Основной класс плагина ---

class SewingFlapPlugin(Gimp.PlugIn):
    def do_query_procedures(self):
        return ["python-fu-sewing-flap"]

    def do_create_procedure(self, name):
        procedure = Gimp.ImageProcedure.new(self, name,
                                           Gimp.PDBProcType.PLUGIN,
                                           self.run, None)
        procedure.set_image_types("*")
        procedure.set_menu_label("Add flap...")
        procedure.add_menu_path('<Image>/Filters/PT')
        procedure.set_documentation(
            "PT Add Flap",
            "Interactive flaps adding by contour paths",
            name)

        # Добавляем параметры в диалог
        # Создаём Choice с тремя вариантами
        side_choice = Gimp.Choice.new()
        side_choice.add("auto",  0, "Авто",   "Определять автоматически")
        side_choice.add("right", 1, "Справа", "Клапаны справа от направления обхода")
        side_choice.add("left",  2, "Слева",  "Клапаны слева от направления обхода")

        # Регистрируем аргумент: name, nick, blurb, choice, value, flags
        procedure.add_choice_argument(
            "side-mode",
            "С_торона",
            "Сторона контура для клапанов при движении от начальной точки к конечной",
            side_choice,          # объект Gimp.Choice
            "auto",               # значение по умолчанию (строка)
            GObject.ParamFlags.READWRITE
        )
        procedure.add_int_argument("height", "В_ысота клапана (px)", "Высота клапана в пикселях",
                                   10, 500, 50, GObject.ParamFlags.READWRITE)
        procedure.add_double_argument("angle", "У_гол при основании (°)", "Угол трапеции при основании",
                                      30.0, 80.0, 60.0, GObject.ParamFlags.READWRITE)
        procedure.add_color_argument("stroke-color", "Ц_вет обводки контура", "Цвет линии границы",
                                      False, Gegl.Color.new("black"), GObject.ParamFlags.READWRITE)
        procedure.add_double_argument("stroke-line-width", "Т_олщина линии обводки (px)", "Толщина линий обводки",
                               1.0, 10.0, 2.0, GObject.ParamFlags.READWRITE)
        procedure.add_color_argument("flap-fill-color", "Цв_ет заливки клапана", "Цвет заливки трапеции",
            False, Gegl.Color.new("rgb(200,200,200)"), GObject.ParamFlags.READWRITE)
        procedure.add_color_argument("flap-outline-color", "Цве_т обводки клапана", "Цвет границы трапеции",
            False, Gegl.Color.new("black"), GObject.ParamFlags.READWRITE)
        procedure.add_double_argument("flap-line-width", "То_лщина линии клапанов (px)", "Толщина линий клапанов",
                               1.0, 10.0, 2.0, GObject.ParamFlags.READWRITE)
                               
        return procedure

    def run(self, procedure, run_mode, image, drawables, config, run_data):
        if not drawables:
            return procedure.new_return_values(Gimp.PDBStatusType.CALLING_ERROR, None)
        layer = drawables[0]

        # --- Диалог ---
        if run_mode == Gimp.RunMode.INTERACTIVE:
            GimpUi.init('python-fu-sewing-flap')
            dialog = GimpUi.ProcedureDialog(procedure=procedure, config=config)

            # Превращаем GimpChoice в радиокнопки ДО заполнения диалога
            dialog.get_widget("side-mode", GimpUi.IntRadioFrame)

            dialog.fill(None)  # Теперь остальные виджеты создаются как обычно
            if not dialog.run():
                dialog.destroy()
                return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, GLib.Error())
            dialog.destroy()

        height          = config.get_property("height")
        angle_deg       = config.get_property("angle")
        base_width      = config.get_property("stroke-line-width")
        base_color      = config.get_property("stroke-color")
        fill_color      = config.get_property("flap-fill-color")
        outline_width   = config.get_property("flap-line-width")
        outline_color   = config.get_property("flap-outline-color")
        side_mode       = config.get_property("side-mode")

        # --- Получаем точки активного контура ---
        paths = image.get_paths()
        if not paths:
            Gimp.message("В изображении нет контуров. Создайте контур (Path).")
            return procedure.new_return_values(Gimp.PDBStatusType.CALLING_ERROR, None)
        active_path = paths[0]

        strokes = active_path.get_strokes()
        if not strokes:
            Gimp.message("Контур не содержит штрихов.")
            return procedure.new_return_values(Gimp.PDBStatusType.CALLING_ERROR, None)

        cps = active_path.stroke_get_points(strokes[0]).controlpoints
        if not cps or len(cps) % 6 != 0:
            Gimp.message("Не удалось разобрать точки контура.")
            return procedure.new_return_values(Gimp.PDBStatusType.CALLING_ERROR, None)
        vertices = [(cps[i], cps[i + 1]) for i in range(0, len(cps), 6)]
        if len(vertices) < 2:
            Gimp.message("Контур должен содержать хотя бы 2 вершины.")
            return procedure.new_return_values(Gimp.PDBStatusType.CALLING_ERROR, None)

        # --- С какой стороны рисуются клапаны ---
        side_choice = config.get_property("side-mode")
        if side_choice == "right":
            outward_sign = 1.0
        elif side_choice == "left":
            outward_sign = -1.0
        else:
            area = 0.0
            n = len(vertices)
            for i in range(n):
                x1, y1 = vertices[i]; x2, y2 = vertices[(i + 1) % n]
                area += x1 * y2 - x2 * y1
            outward_sign = -1.0 if area > 0 else 1.0
        Gimp.message("Клапаны: " +
                     ("справа" if outward_sign > 0 else "слева") +
                     " от направления обхода.")

        # --- Углы по вершинам ---
        n = len(vertices)
        vertex_angles = [angle_deg] * n
        for i in range(1, n - 1):
            vertex_angles[i] = compute_adaptive_angle(
                vertices[i - 1], vertices[i], vertices[i + 1], angle_deg)

        # --- Отрисовка ---
        image.undo_group_start()
        try:
            temp_layer = Gimp.Layer.new(
                image, "Sewing Flaps Temp",
                image.get_width(), image.get_height(),
                Gimp.ImageType.RGBA_IMAGE, 100, Gimp.LayerMode.NORMAL)
            image.insert_layer(temp_layer, None, 0)

            for i in range(n - 1):
                draw_flap_on_layer(
                    layer, temp_layer,
                    vertices[i], vertices[i + 1],
                    height,
                    vertex_angles[i], vertex_angles[i + 1],
                    fill_color, base_color, base_width, outline_color, outline_width,
                    outward_sign,
                )

            image.merge_down(temp_layer, Gimp.MergeType.EXPAND_AS_NECESSARY)
            Gimp.displays_flush()
        except Exception as e:
            image.undo_group_end()
            Gimp.message(f"Ошибка выполнения плагина: {e}")
            return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error())
        finally:
            image.undo_group_end()

        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, None)

Gimp.main(SewingFlapPlugin.__gtype__, sys.argv)
