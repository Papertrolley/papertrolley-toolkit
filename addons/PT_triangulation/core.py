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
import numpy as np


def make_K(f_px, W, H):
    return np.array([
        [f_px, 0.0, W / 2.0],
        [0.0, f_px, H / 2.0],
        [0.0, 0.0, 1.0],
    ])


def make_P(matrix_world, f_px, W, H):
    """P (3x4) из matrix_world камеры Blender.

    matrix_world — либо 4x4 numpy-массив, либо плоский список из 16.
    """
    M = np.asarray(matrix_world, dtype=np.float64)
    if M.size == 16:
        M = M.reshape(4, 4)

    M_inv = np.linalg.inv(M)
    D = np.diag([1.0, -1.0, -1.0, 1.0])
    M_cv = D @ M_inv
    K = make_K(f_px, W, H)
    return K @ M_cv[:3, :]


def undistort_pixel(u, v, f_px, W, H, k1, k2, iters=20):
    """Из дистортированного пикселя — недистортированный."""
    cx, cy = W / 2.0, H / 2.0
    xn = (u - cx) / f_px
    yn = (v - cy) / f_px
    xu, yu = xn, yn
    for _ in range(iters):
        r2 = xu * xu + yu * yu
        fac = 1.0 + k1 * r2 + k2 * r2 * r2
        if abs(fac) < 1e-9:
            break
        nxu = xn / fac
        nyu = yn / fac
        if abs(nxu - xu) < 1e-9 and abs(nyu - yu) < 1e-9:
            xu, yu = nxu, nyu
            break
        xu, yu = nxu, nyu
    return xu * f_px + cx, yu * f_px + cy


def triangulate(cameras, points_2d):
    """Linear DLT-триангуляция.

    cameras: список dict {'P', 'f_px', 'W', 'H', 'k1', 'k2'}
    points_2d: список (u, v) — дистортированные пиксели
    Возвращает np.array([x, y, z]) или None.
    """
    if len(cameras) < 2:
        return None

    A = []
    for cam, (u_d, v_d) in zip(cameras, points_2d):
        u, v = undistort_pixel(u_d, v_d, cam['f_px'], cam['W'], cam['H'],
                                cam['k1'], cam['k2'])
        P = cam['P']
        A.append(u * P[2] - P[0])
        A.append(v * P[2] - P[1])
    A = np.array(A)

    _, _, Vt = np.linalg.svd(A)
    X = Vt[-1]
    if abs(X[3]) < 1e-12:
        return None
    return X[:3] / X[3]
