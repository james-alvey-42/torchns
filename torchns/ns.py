# Authors:
# Christoph Weniger <c.weniger@uva.nl>, June, July 2023
# Noemi Anau Montel <n.anaumontel@uva.nl>, June, July, August 2023

import torch
import numpy as np
from tqdm.auto import tqdm


class NestedSampler:
    def __init__(self, X_init, L_init=None, bound="unitcube"):
        self.X_init = X_init
        self.X_live = X_init * 1.0
        self.L_live = L_init if L_init is not None else None
        self.device = X_init.device

        if bound == "unitcube":
            self._inbound = self._inbound_unitcube
        elif callable(bound):
            self._inbound = bound
        else:
            raise KeyError("Bound unknown")
        assert all(self._inbound(X_init)), "X_init not within specified bounds"

    def _inbound_unitcube(self, X):
        """This function checks if samples are inside boundaries."""
        return (X <= 1).prod(-1) * (X >= 0).prod(-1)

    def _get_directions(self, B, D):
        """This function generates a minibatch of D-dimensional random directions."""
        t = torch.randn(B, D, device=self.device)
        l = (t**2).sum(axis=-1) ** 0.5
        n = t / l.unsqueeze(-1)
        return n

    def _get_slice_sample_points(self, B, S):
        """This function generates a minibatch of S slice sample positions."""
        hard_bounds = (
            torch.tensor([-1.0, 1.0], device=self.device).unsqueeze(0).repeat((B, 1))
        )
        current_bounds = hard_bounds.clone()
        L = torch.empty((B, S), device=self.device)
        for i in range(S):
            x = (
                torch.rand(B, device=self.device)
                * (current_bounds[:, 1] - current_bounds[:, 0])
                + current_bounds[:, 0]
            )
            L[:, i] = x
            current_bounds[x < 0, 0] = x[x < 0]
            current_bounds[x > 0, 1] = x[x > 0]
        return L

    def _gen_new_samples(
        self,
        X_seeds,
        logl_fn,
        logl_th,
        num_steps=3,
        max_step_size=1.0,
        samples_per_slice=5,
        Lchol=None,
    ):
        """This function generates new samples within the likelihood constraint logl_fn > log_th."""
        if Lchol is None:
            Lchol = self._calc_Lchol(X_seeds)
        B, D = X_seeds.shape
        C = torch.zeros(B, device=self.device)  # counter for accepted points
        X = X_seeds.clone()
        logl = torch.ones(B, device=self.device) * (-np.inf)
        for i in range(num_steps):
            N = self._get_directions(B, D)
            L = self._get_slice_sample_points(B, S=samples_per_slice) * max_step_size
            dX_uniform = N.unsqueeze(-2) * L.unsqueeze(-1)
            dX = torch.matmul(dX_uniform, Lchol.T.to(self.device))
            pX = X.unsqueeze(-2) + dX  # proposals
            pX_shape = pX.shape
            pX2 = pX.flatten(0, -2)
            logl_prop = logl_fn(pX2).view(*pX_shape[:-2], -1)
            inbound = self._inbound(pX2).view(*pX_shape[:-2], -1)
            accept_matrix = ((logl_prop > logl_th) * inbound).bool()
            idx = torch.argmax(accept_matrix.int(), dim=1)
            nX = torch.stack([pX[i][idx[i]] for i in range(B)], dim=0)
            logl_selected = torch.stack([logl_prop[i][idx[i]] for i in range(B)], dim=0)
            accept_any = accept_matrix.sum(dim=-1) > 0
            X[accept_any] = nX[accept_any]
            logl[accept_any] = logl_selected[accept_any]
            C[accept_any] += 1
        return X[C == num_steps], logl[C == num_steps]

    def nested_sampling(
        self,
        logl_fn,
        logl_th_max=np.inf,
        max_steps=100000,
        num_batch_samples=200,
        epsilon=1e-6,
        max_step_size=1.0,
        samples_per_slice=10,
        num_steps=5,
    ):
        """Run nested sampling, staring with X_init live points."""
        X_init = self.X_live
        NLP, D = X_init.shape
        X_live = X_init.clone()
        if self.L_live is None:
            L_live = logl_fn(X_live)
        else:
            L_live = self.L_live
        B = min(num_batch_samples, NLP)  # Number of samples generated simultanously
        V = 1.0  # initial volume is set to 1.
        samples_X = []
        samples_logl = []  # logl values
        samples_logv = []  # estimate of constrained volume
        samples_Z = []
        samples_logwt = []
        logl_th = torch.tensor(-np.inf)
        Z = 0
        Z_rest = torch.tensor(np.inf)

        pbar = tqdm(range(max_steps))
        for i in pbar:
            pbar.set_description(
                "Z_sum=%.2e, Z_rest=%.2e, logl_min=%.2f"
                % (Z, Z_rest.item(), logl_th.item())
            )
            idx_batch = np.random.choice(range(NLP), B, replace=True)
            X_batch = X_live[idx_batch]
            logl_th = L_live.min()
            if (
                logl_th > logl_th_max
            ):  # Stop sampling once maxmimum threshold is reached
                break
            Lchol = self._calc_Lchol(X_live)
            X_new, L_new = self._gen_new_samples(
                X_batch,
                logl_fn,
                logl_th,
                num_steps=num_steps,
                Lchol=Lchol,
                max_step_size=max_step_size,
                samples_per_slice=samples_per_slice,
            )

            for i in range(len(X_new)):
                if L_new[i] > L_live.min():
                    idx_min = np.argmin(L_live.cpu().numpy())
                    Lmin = L_live[idx_min].item() * 1.0
                    samples_X.append(1.0 * X_live[idx_min].cpu().numpy())
                    samples_logl.append(Lmin)
                    samples_logv.append(np.log(V))
                    L_live[idx_min] = L_new[i] * 1.0
                    X_live[idx_min] = X_new[i] * 1.0
                    V *= 1 - 1 / NLP  # Volume estimate per sample
                    dZ = V / NLP * np.exp(Lmin)
                    samples_Z.append(dZ)
                    samples_logwt.append(Lmin + np.log(V / NLP))
                    Z = Z + dZ
                    Z_rest = V * torch.exp(L_live.max() * 1.0)
                else:
                    break
            if Z_rest < Z * epsilon:
                break
        samples_logv = torch.tensor(np.array(samples_logv)).float()
        samples_logl = torch.tensor(np.array(samples_logl)).float()
        samples_X = torch.tensor(np.array(samples_X)).float()
        samples_logwt = torch.tensor(np.array(samples_logwt)).float()
        self.X_live = X_live
        self.L_live = L_live
        self.samples_X = samples_X
        self.samples_logv = samples_logv
        self.samples_logl = samples_logl
        self.samples_logwt = samples_logwt

    def generate_constrained_prior_samples(
        self, logl_fn, N, min_logl=-np.inf, batch_size=100, num_steps=10
    ):
        """This function generates new constrained prior samples inside the iso-contour defined by min_logl."""
        X_seeds, _ = self.get_constrained_prior_samples(N, min_logl=min_logl)
        X_seeds = X_seeds.to(self.device)
        X_samples = []
        L_samples = []
        Lchol = self._calc_Lchol(X_seeds)
        for i in tqdm(range(N // batch_size)):
            X_batch = X_seeds[i * batch_size : (i + 1) * batch_size]
            X_new, L_new = self._gen_new_samples(
                X_batch, logl_fn, min_logl, num_steps=num_steps
            )
            X_samples.append(X_new)
            L_samples.append(L_new)
        return torch.cat(X_samples), torch.cat(L_samples)

    def get_threshold(self, p):
        """This function defines defines the convergence criterion related to posterior mass."""
        wt = np.exp(self.samples_logwt)
        wt /= wt.sum()
        cwt = np.cumsum(wt)
        return np.interp(p, cwt, self.samples_logl)

    def get_posterior_samples(self, N=None):
        """This function generates posterior samples."""
        if N is None:
            N = int(self.get_posterior_neff())
        logwt = self.samples_logwt
        wt = torch.exp(logwt - logwt.max())
        wt /= wt.sum()
        idx = torch.multinomial(wt, N, replacement=True)
        return self.samples_X[idx], self.samples_logl[idx]

    def get_posterior_neff(self):
        """This function returns the number of effective posterior samples."""
        logwt = self.samples_logwt
        wt = torch.exp(logwt - logwt.max())
        wt /= wt.sum()
        n_eff = sum(wt) ** 2 / sum(wt**2)
        return n_eff.item()

    def get_constrained_prior_samples(self, N=None, min_logl=-np.inf):
        """This function generates constrained prior samples."""
        if N is None:
            N = int(self.get_constrained_prior_neff(min_logl))
        logv = self.samples_logv
        logl = self.samples_logl
        mask = logl >= min_logl
        wt = torch.exp(logv[mask] - logv[mask].max())
        idx = torch.multinomial(wt, N, replacement=True)
        return self.samples_X[mask][idx], self.samples_logl[mask][idx]

    def get_constrained_prior_neff(self, min_logl):
        """This function returns the number of effective constrained prior samples inside the iso-contour defined by min_logl."""
        logv = self.samples_logv
        logl = self.samples_logl
        mask = logl >= min_logl
        wt = torch.exp(logv[mask] - logv[mask].max())
        n_eff = sum(wt) ** 2 / sum(wt**2)
        return n_eff.item()

    def _calc_Lchol(self, X):
        """Estimate Cholesky decomposition of X covariance"""
        cov = torch.cov(X.T)
        try:  # Deal with negative covariance matrices
            L = torch.linalg.cholesky(cov)
        except torch.linalg.LinAlgError:
            eigvals = torch.linalg.eigvalsh(cov)
            mineig = eigvals.min()
            cov = cov - 2 * mineig * torch.eye(len(cov), device=mineig.device)
            L = torch.linalg.cholesky(cov)
        return L
