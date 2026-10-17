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


def _rodrigues(r):
    theta = np.linalg.norm(r)
    if theta < 1e-12:
        return np.eye(3)
    k = r / theta
    K = np.array([
        [0, -k[2], k[1]],
        [k[2], 0, -k[0]],
        [-k[1], k[0], 0],
    ])
    return (np.eye(3) + np.sin(theta) * K
            + (1 - np.cos(theta)) * (K @ K))


def _residuals_fixed_f(params, world_pts, pixel_pts, W, H, f_px):
    """params: [rx, ry, rz, tx, ty, tz]. Без дисторсии."""
    R_wc = _rodrigues(params[0:3])
    t_wc = params[3:6]
    cx, cy = W / 2.0, H / 2.0
    res = np.zeros(2 * len(world_pts))
    for i, (X, xp) in enumerate(zip(world_pts, pixel_pts)):
        Xc = R_wc @ np.asarray(X) + t_wc
        if Xc[2] <= 1e-9:
            res[2 * i] = 1e6
            res[2 * i + 1] = 1e6
            continue
        u = f_px * Xc[0] / Xc[2] + cx
        v = f_px * Xc[1] / Xc[2] + cy
        res[2 * i] = u - xp[0]
        res[2 * i + 1] = v - xp[1]
    return res


def _residuals_with_distortion(params, world_pts, pixel_pts, W, H, f_px, use_k1=True, use_k2=False):
    """params: [rx, ry, rz, tx, ty, tz, k1, k2]. Только радиальная k1-k2."""
    R_wc = _rodrigues(params[0:3])
    t_wc = params[3:6]
    k1 = params[6]
    k2 = params[7]
    cx, cy = W / 2.0, H / 2.0
    res = np.zeros(2 * len(world_pts))
    for i, (X, xp) in enumerate(zip(world_pts, pixel_pts)):
        Xc = R_wc @ np.asarray(X) + t_wc
        if Xc[2] <= 1e-9:
            res[2 * i] = 1e6
            res[2 * i + 1] = 1e6
            continue
        xn = Xc[0] / Xc[2]
        yn = Xc[1] / Xc[2]
        r2 = xn * xn + yn * yn
        fac = 1.0 + k1 * r2 if use_k1 else 1.0
        fac += k2 * r2 * r2 if use_k2 else 0.0
        u = f_px * xn * fac + cx
        v = f_px * yn * fac + cy
        res[2 * i] = u - xp[0]
        res[2 * i + 1] = v - xp[1]
        if abs(k1) > 0.15:
            res = res + 1e4 * np.sign(res)
        if abs(k2) > 0.05:
            res = res + 1e4 * np.sign(res)
    return res


def _matrix_from_pose(params):
    """[rx, ry, rz, tx, ty, tz] → matrix_world (Blender-конвенция)."""
    R_wc = _rodrigues(params[0:3])
    t_wc = params[3:6]
    D = np.diag([1.0, -1.0, -1.0])
    R_cw_cv = R_wc.T
    t_cw = -R_cw_cv @ t_wc
    R_cw_bl = R_cw_cv @ D
    M = np.eye(4)
    M[:3, :3] = R_cw_bl
    M[:3, 3] = t_cw
    return M


def _params_from_matrix(M):
    """matrix_world → [rx, ry, rz, tx, ty, tz]."""
    R_cw_bl = np.asarray(M[:3, :3])
    t_cw = np.asarray(M[:3, 3])
    D = np.diag([1.0, -1.0, -1.0])
    R_cw_cv = R_cw_bl @ D
    R_wc_cv = R_cw_cv.T
    t_wc_cv = -R_wc_cv @ t_cw

    theta = np.arccos(np.clip((np.trace(R_wc_cv) - 1) / 2.0, -1.0, 1.0))
    if theta < 1e-9:
        rvec = np.zeros(3)
    else:
        k = np.array([
            R_wc_cv[2, 1] - R_wc_cv[1, 2],
            R_wc_cv[0, 2] - R_wc_cv[2, 0],
            R_wc_cv[1, 0] - R_wc_cv[0, 1],
        ]) / (2.0 * np.sin(theta))
        rvec = k * theta
    return np.concatenate([rvec, t_wc_cv])


def _huber_cost(res, delta):
    if delta <= 0:
        return float(0.5 * (res @ res))
    abs_r = np.abs(res)
    quad = 0.5 * res * res
    lin = delta * (abs_r - 0.5 * delta)
    return float(np.sum(np.where(abs_r <= delta, quad, lin)))


def _lm_solve(cost, params_init, n_params, max_iter=100, huber_delta=0.0):
    """LM с опциональным Huber loss."""
    params = params_init.copy()
    res = cost(params)
    err = _huber_cost(res, huber_delta)
    lam = 1e-3

    for _it in range(max_iter):
        J = np.zeros((len(res), n_params))
        eps = 1e-6
        for k in range(n_params):
            dp = params.copy()
            dp[k] += eps
            r2 = cost(dp)
            J[:, k] = (r2 - res) / eps

        if huber_delta > 0:
            abs_r = np.abs(res)
            w = np.where(abs_r <= huber_delta, 1.0,
                          huber_delta / np.maximum(abs_r, 1e-12))
            H_ = J.T @ (J * w[:, None])
            g = J.T @ (res * w)
        else:
            H_ = J.T @ J
            g = J.T @ res

        accepted = False
        delta = np.zeros(n_params)
        for _ in range(15):
            try:
                delta = np.linalg.solve(H_ + lam * np.eye(n_params), -g)
            except np.linalg.LinAlgError:
                lam *= 10.0
                continue
            new_p = params + delta
            new_res = cost(new_p)
            new_err = _huber_cost(new_res, huber_delta)
            if new_err < err:
                params = new_p
                res = new_res
                err = new_err
                lam = max(lam / 3.0, 1e-9)
                accepted = True
                break
            lam *= 5.0

        if not accepted:
            break
        if np.linalg.norm(delta) < 1e-10:
            break

    # rmse считаем по обычной сумме квадратов — для отчёта
    plain_err = float(res @ res)
    return params, res, plain_err


def pose_refine(world_pts, pixel_pts, W, H, f_px, M_init, huber_delta=0.0, max_iter=100):
    """LM по 6 параметрам позы. Фокусное фиксировано. Без дисторсии."""
    p0 = _params_from_matrix(M_init)
    t_scale = max(float(np.linalg.norm(p0[3:6])), 0.1)

    def unscale(ps):
        return np.concatenate([ps[0:3], ps[3:6] * t_scale])

    def cost(ps):
        return _residuals_fixed_f(unscale(ps), world_pts, pixel_pts,
                                   W, H, f_px)

    params0 = np.concatenate([p0[0:3], p0[3:6] / t_scale])
    final, _, err = _lm_solve(cost, params0, 6, max_iter,
                               huber_delta=huber_delta)

    M = _matrix_from_pose(unscale(final))
    rmse = float(np.sqrt(err / len(world_pts)))
    return M, rmse


def pose_refine_with_distortion(world_pts, pixel_pts, W, H, f_px,
                                  M_init, huber_delta=0.0, use_k1=True, use_k2=False, max_iter=150):
    """LM по 7-8 параметрам: 6 позы + k1/k2. Фокусное фиксировано.
    Возвращает (M, rmse, k1, k2)."""
    p0 = _params_from_matrix(M_init)
    t_scale = max(float(np.linalg.norm(p0[3:6])), 0.1)

    def unscale(ps):
        return np.concatenate([
            ps[0:3],
            ps[3:6] * t_scale,
            [ps[6]],  # k1 как есть (и так малый)
            [ps[7]],  # k2 как есть (и так малый)
        ])

    def cost(ps):
        return _residuals_with_distortion(unscale(ps), world_pts,
                                            pixel_pts, W, H, f_px, use_k1=use_k1, use_k2=use_k2)

    params0 = np.concatenate([
        p0[0:3], p0[3:6] / t_scale, [0.0], [0.0],
    ])
    final, _, err = _lm_solve(cost, params0, 8, max_iter,
                               huber_delta=huber_delta)

    unscaled = unscale(final)
    M = _matrix_from_pose(unscaled[0:6])
    k1 = float(unscaled[6])
    k2 = float(unscaled[7])
    rmse = float(np.sqrt(err / len(world_pts)))
    return M, rmse, k1, k2


def _make_candidates(M_init):
    def _rot(axis, deg):
        a = np.radians(deg)
        c, s = np.cos(a), np.sin(a)
        if axis == 0:
            return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
        if axis == 1:
            return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
        return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])

    out = [np.asarray(M_init)]
    for axis in (0, 1, 2):
        for deg in (10, -10, 25, -25):
            R = _rot(axis, deg)
            Mp = np.asarray(M_init).copy()
            Mp[:3, :3] = Mp[:3, :3] @ R
            out.append(Mp)
    return out


def pose_refine_multistart(world_pts, pixel_pts, W, H, f_px, M_init,
                            use_distortion_k1=False, use_distortion_k2=False,
                            verbose=False, huber_delta=0.0):
    """Многозапусковый LM. Возвращает (M, rmse, k1, k2)."""
    candidates = _make_candidates(M_init)
    results = []
    best_M = None
    best_rmse = float('inf')
    best_k1 = 0.0
    best_k2 = 0.0

    for M_c in candidates:
        if use_distortion_k1 or use_distortion_k2:
            M_r, rmse, k1, k2 = pose_refine_with_distortion(
                world_pts, pixel_pts, W, H, f_px, M_c, huber_delta=huber_delta, use_k1=use_distortion_k1, use_k2=use_distortion_k2)
        else:
            M_r, rmse = pose_refine(
                world_pts, pixel_pts, W, H, f_px, M_c, huber_delta=huber_delta)
            k1 = 0.0
            k2 = 0.0
        results.append(rmse)
        if rmse < best_rmse:
            best_rmse = rmse
            best_M = M_r
            best_k1 = k1
            best_k2 = k2

    if verbose:
        print(f"[PBA multistart] {len(candidates)} запусков, "
              f"rmse от {min(results):.3f} до {max(results):.3f}")

    return best_M, best_rmse, best_k1, best_k2
