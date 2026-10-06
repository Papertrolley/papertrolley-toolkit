import numpy as np


def solve_pnp(world_pts, pixel_pts, K):
    """PnP через DLT. Возвращает matrix_world камеры (4x4 numpy)
    или None при неудаче."""
    N = len(world_pts)
    if N < 6:
        return None

    K_inv = np.linalg.inv(K)

    A = np.zeros((2 * N, 12))
    for i in range(N):
        X = world_pts[i]
        u_px, v_px = pixel_pts[i]
        x_n = K_inv @ np.array([u_px, v_px, 1.0])
        u, v, w = x_n
        X_h = np.array([X[0], X[1], X[2], 1.0])
        A[2 * i]     = np.concatenate([np.zeros(4), -w * X_h,  v * X_h])
        A[2 * i + 1] = np.concatenate([ w * X_h, np.zeros(4), -u * X_h])

    _, _, Vt = np.linalg.svd(A)
    P = Vt[-1].reshape(3, 4)

    M = P[:, :3]
    t_raw = P[:, 3]

    U, S, V2t = np.linalg.svd(M)
    R_cv = U @ V2t
    scale = S.mean()
    if scale < 1e-12:
        return None
    t_cv = t_raw / scale

    if np.linalg.det(R_cv) < 0:
        R_cv = -R_cv
        t_cv = -t_cv

    # Проверка: объект должен быть перед камерой (Z > 0 в OpenCV)
    X0 = R_cv @ np.array(world_pts[0]) + t_cv
    if X0[2] < 0:
        R_cv = -R_cv
        t_cv = -t_cv

    # OpenCV (x→right, y→down, z→forward) → Blender (x→right, y→up, z→back)
    D = np.diag([1.0, -1.0, -1.0])
    R_bl = D @ R_cv
    t_bl = D @ t_cv

    M_cam = np.eye(4)
    M_cam[:3, :3] = R_bl
    M_cam[:3, 3] = t_bl

    return np.linalg.inv(M_cam)
    

def refine_focal_and_pose(world_pts, pixel_pts, W, H, f_px_initial):
    """Grid search по фокусному. Возвращает (matrix_world, f_px_best, rmse_px)
    или (None, None, None). Логирует ошибку по сетке."""
    N = len(world_pts)

    def rmse_at(f_px):
        K = np.array([
            [f_px, 0.0, W / 2.0],
            [0.0, f_px, H / 2.0],
            [0.0, 0.0, 1.0],
        ])
        M = solve_pnp(world_pts, pixel_pts, K)
        if M is None:
            return None, float('inf')
        # M — camera-to-world в Blender-конвенции (z смотрит назад).
        # Переводим в OpenCV-конвенцию (z вперёд, y вниз), чтобы
        # корректно применять K и сравнивать с pixel_pts.
        D_cv = np.diag([1.0, -1.0, -1.0, 1.0])
        P_cv = D_cv @ np.linalg.inv(M)
        total = 0.0
        for X, xp in zip(world_pts, pixel_pts):
            X_h = np.array([X[0], X[1], X[2], 1.0])
            proj = K @ (P_cv[:3, :] @ X_h)
            if proj[2] <= 1e-9:
                return M, float('inf')
            proj_2d = proj[:2] / proj[2]
            d = proj_2d - np.asarray(xp)
            total += float(d @ d)
        return M, float(np.sqrt(total / N))

    # Грубая сетка в лог-пространстве: от 0.25 до 4.0 × f_init
    import math
    lo, hi = 0.25, 4.0
    steps = 25
    results = []
    best_f, best_err, best_M = None, float('inf'), None
    for i in range(steps):
        t = i / (steps - 1)
        f = f_px_initial * math.exp(math.log(lo) * (1 - t)
                                     + math.log(hi) * t)
        M, err = rmse_at(f)
        results.append((f, err))
        if M is not None and err < best_err:
            best_f, best_err, best_M = f, err, M

    print("[PBA refine] f_px  →  RMSE (px):")
    for f, err in results:
        marker = "  <-- best" if f == best_f else ""
        print(f"  {f:8.1f}  →  {err:8.2f}{marker}")
    print(f"[PBA refine] f_init = {f_px_initial:.1f}, "
          f"f_best = {best_f:.1f}, RMSE = {best_err:.2f}")

    if best_M is None:
        return None, None, None
    return best_M, best_f, best_err
    

def _rodrigues(r):
    """r: (3,) вектор вращения → R: (3,3)."""
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


def _residuals(params, world_pts, pixel_pts, W, H):
    """params: [rx, ry, rz, tx, ty, tz, f_px].
    Параметры описывают world → camera в OpenCV-конвенции
    (X right, Y down, Z forward)."""
    rvec = params[0:3]
    tvec = params[3:6]
    f = params[6]

    if f <= 0.0:
        return np.full(2 * len(world_pts), 1e6)

    R_wc = _rodrigues(rvec)
    cx, cy = W / 2.0, H / 2.0

    res = np.zeros(2 * len(world_pts))
    for i, (X, xp) in enumerate(zip(world_pts, pixel_pts)):
        Xc = R_wc @ np.asarray(X) + tvec
        if Xc[2] <= 1e-9:
            res[2 * i] = 1e6
            res[2 * i + 1] = 1e6
            continue
        u = f * Xc[0] / Xc[2] + cx
        v = f * Xc[1] / Xc[2] + cy
        res[2 * i] = u - xp[0]
        res[2 * i + 1] = v - xp[1]
    return res


def _matrix_from_params(params):
    """[rx, ry, rz, tx, ty, tz, f] → (matrix_world 4x4, f_px)."""
    rvec = params[0:3]
    tvec = params[3:6]
    f = params[6]
    R_wc = _rodrigues(rvec)          # world → camera (OpenCV-style)
    t_wc = tvec

    # Blender camera смотрит в -Z; конвертация:
    D = np.diag([1.0, -1.0, -1.0])
    R_cw_cv = R_wc.T                 # camera → world в CV
    R_cw_bl = D @ R_cw_cv            # camera → world в Blender
    t_cw = -R_cw_cv @ t_wc           # положение камеры в мире

    M = np.eye(4)
    M[:3, :3] = R_cw_bl
    M[:3, 3] = t_cw
    return M, f


def geometric_refine(world_pts, pixel_pts, W, H, M_init, f_px_init,
                     max_iter=60):
    """LM поверх DLT. M_init — matrix_world в Blender-конвенции."""

    # --- Извлечение стартовых параметров ---
    R_cw_bl = np.asarray(M_init[:3, :3])
    t_cw = np.asarray(M_init[:3, 3])
    D = np.diag([1.0, -1.0, -1.0])

    # camera→world в CV: R_cw_cv = R_cw_bl @ D
    R_cw_cv = R_cw_bl @ D
    R_wc_cv = R_cw_cv.T
    t_wc_cv = -R_wc_cv @ t_cw

    # Матрица вращения → вектор Родрига
    theta = np.arccos(np.clip((np.trace(R_wc_cv) - 1) / 2.0, -1.0, 1.0))
    if theta < 1e-9:
        rvec0 = np.zeros(3)
    else:
        k = np.array([
            R_wc_cv[2, 1] - R_wc_cv[1, 2],
            R_wc_cv[0, 2] - R_wc_cv[2, 0],
            R_wc_cv[1, 0] - R_wc_cv[0, 1],
        ]) / (2.0 * np.sin(theta))
        rvec0 = k * theta

    # Масштабы для нормализации параметров
    t_scale = max(float(np.linalg.norm(t_wc_cv)), 0.1)
    f_scale = float(f_px_init)

    def unscale(ps):
        return np.concatenate([
            ps[0:3],
            ps[3:6] * t_scale,
            [ps[6] * f_scale],
        ])

    def cost(ps):
        return _residuals(unscale(ps), world_pts, pixel_pts, W, H)

    params = np.concatenate([rvec0, t_wc_cv / t_scale, [1.0]])
    res = cost(params)
    err = float(res @ res)

    lam = 1e-3
    for _it in range(max_iter):
        J = np.zeros((len(res), 7))
        eps = 1e-6
        for k in range(7):
            dp = params.copy()
            dp[k] += eps
            r2 = cost(dp)
            J[:, k] = (r2 - res) / eps

        H_ = J.T @ J
        g = J.T @ res

        accepted = False
        delta = np.zeros(7)
        for _ in range(15):
            try:
                delta = np.linalg.solve(H_ + lam * np.eye(7), -g)
            except np.linalg.LinAlgError:
                lam *= 10.0
                continue

            new_p = params + delta
            # Фокусное должно остаться положительным
            if new_p[6] <= 0.01:
                lam *= 10.0
                continue

            new_res = cost(new_p)
            new_err = float(new_res @ new_res)
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

    # --- Обратное преобразование ---
    final = unscale(params)
    f_px = float(final[6])
    R_wc_cv = _rodrigues(final[0:3])
    t_wc_cv = final[3:6]

    R_cw_cv = R_wc_cv.T
    t_cw = -R_cw_cv @ t_wc_cv
    R_cw_bl = R_cw_cv @ D

    M = np.eye(4)
    M[:3, :3] = R_cw_bl
    M[:3, 3] = t_cw

    rmse = float(np.sqrt(err / len(world_pts)))
    return M, f_px, rmse
