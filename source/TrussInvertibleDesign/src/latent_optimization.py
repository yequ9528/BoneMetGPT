"""2026-11-10 delivery, adapted only for explicit CPU RNG and structured output.

Search sizes, losses, Adam steps and diversity selection match the delivered
main_export_test.py. This module performs no I/O or model loading on import.
"""
import numpy as np
import torch

VERSION = 'truss_latent_20261110'
TOP_K = 3
N_TRAJ, STEPS1, LR1 = 1024, 300, .01
N_TRAJ2, STEPS2, LR2 = 512, 200, .004
NOISE, REG = .25, 1e-3
D_MIN_LIST = (.16, .11, .08, .055, .035, .02)
E_TC4 = 110.


def select_diverse(theta, nmse):
    if len(theta) < TOP_K or not np.isfinite(theta).all() or not np.isfinite(nmse).all():
        raise ValueError('optimizer produced insufficient or nonfinite candidates')
    order = np.argsort(nmse)
    for threshold in D_MIN_LIST:
        picks = []
        for index in order:
            if len(picks) >= TOP_K:
                break
            if all(np.linalg.norm(theta[index] - theta[j]) >= threshold for j in picks):
                picks.append(int(index))
        if len(picks) == TOP_K:
            return picks, threshold
    return [int(i) for i in order[:TOP_K]], 0.


def optimize_one(c_abs, *, inverse_model, forward_model, scaler_x, scaler_y, generator):
    values = np.asarray(c_abs, dtype=np.float64)
    if values.shape != (9,) or not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError('target must contain nine finite positive stiffness values in GPa')
    tn = torch.tensor((values / E_TC4)[None, :], dtype=torch.float32, device='cpu')
    cond = scaler_y.normalize(tn)
    scale, shift = scaler_y.max - scaler_y.min, scaler_y.min
    den, latent = (tn ** 2).sum(), inverse_model.latent_dim
    if not torch.isfinite(den) or den <= 0:
        raise ValueError('target norm is not representable in float32')

    def refine(z, steps, lr):
        optimizer = torch.optim.Adam([z], lr=lr)
        best = torch.full((len(z),), float('inf'))
        best_z = z.detach().clone()
        for _ in range(steps):
            optimizer.zero_grad()
            prediction = forward_model(inverse_model.decode(z, cond.repeat(len(z), 1))) * scale + shift
            nmse = ((prediction - tn) ** 2).sum(1) / den
            (nmse.mean() + REG * (z ** 2).sum(1).mean() / latent).backward()
            with torch.no_grad():
                update = nmse < best
                best[update], best_z[update] = nmse[update], z.detach()[update]
            optimizer.step()
        return best, best_z

    z = torch.randn(N_TRAJ, latent, generator=generator).requires_grad_(True)
    best, best_z = refine(z, STEPS1, LR1)
    top8 = torch.argsort(best)[:8]
    z0 = best_z[top8].repeat_interleave(max(N_TRAJ2 // 8, 1), dim=0)
    z2 = (z0 + NOISE * torch.randn(z0.shape, dtype=z0.dtype, generator=generator)).detach().requires_grad_(True)
    _, b2z = refine(z2, STEPS2, LR2)
    with torch.no_grad():
        z_all = torch.cat([best_z, b2z], dim=0)
        x_all = inverse_model.decode(z_all, cond.repeat(len(z_all), 1))
        prediction = forward_model(x_all) * scale + shift
        nmse = (((prediction - tn) ** 2).sum(1) / den).numpy()
        predicted_gpa = prediction.numpy() * E_TC4
        theta = scaler_x.unnormalize(x_all).numpy()
    if not np.isfinite(predicted_gpa).all():
        raise ValueError('optimizer produced nonfinite predictions')
    picks, threshold = select_diverse(theta, nmse)
    return [(theta[i], predicted_gpa[i], float(nmse[i])) for i in picks], threshold
