"""Linear classifiers on hashed sparse features, in numpy alone.

Small on purpose: the models here have a few thousand rows and tens of
thousands of (hashed) feature columns, so a sparse matrix of three arrays,
two loss functions and an L-BFGS loop are all that's needed. scikit-learn
would have done the same job for about 190 MB of extra image (it brings
SciPy); numpy is already in the image through OpenCV.

Weights are saved as plain arrays (.npz, read with allow_pickle=False), so a
model file can't carry code.
"""
from __future__ import annotations

import zlib

import numpy as np

DIM = 2 ** 15          # hashed feature columns
BIAS = "__bias__"


def feature_index(name: str, dim: int = DIM) -> int:
    """A stable column for a feature name. Python's hash() changes between
    runs, so CRC32 is used instead: the same name lands in the same column
    in the process that trained the model and the one that uses it."""
    return zlib.crc32(name.encode("utf-8")) % dim


class Sparse:
    """Rows of {column: value}, stored the way CSR matrices are."""

    def __init__(self, rows: list[dict[str, float]], dim: int = DIM):
        self.dim = dim
        indptr = [0]
        indices: list[int] = []
        data: list[float] = []
        for feats in rows:
            merged: dict[int, float] = {}
            for name, value in feats.items():
                if value:
                    col = feature_index(name, dim)
                    merged[col] = merged.get(col, 0.0) + float(value)
            merged[feature_index(BIAS, dim)] = 1.0
            indices.extend(merged.keys())
            data.extend(merged.values())
            indptr.append(len(indices))
        self._set(np.asarray(indptr, dtype=np.int64), np.asarray(indices, dtype=np.int64),
                  np.asarray(data, dtype=np.float64))

    def _set(self, indptr, indices, data):
        self.indptr, self.indices, self.data = indptr, indices, data
        self.n = len(indptr) - 1
        self.row_of = np.repeat(np.arange(self.n), np.diff(indptr))

    def take(self, rows) -> "Sparse":
        rows = np.asarray(rows, dtype=np.int64)
        out = Sparse.__new__(Sparse)
        out.dim = self.dim
        starts, ends = self.indptr[rows], self.indptr[rows + 1]
        lengths = ends - starts
        indptr = np.concatenate([[0], np.cumsum(lengths)])
        pick = np.concatenate([np.arange(s, e) for s, e in zip(starts, ends)]) if len(rows) else np.zeros(0, np.int64)
        out._set(indptr, self.indices[pick], self.data[pick])
        return out

    def dot(self, W: np.ndarray) -> np.ndarray:
        """X @ W, W of shape (dim, k)."""
        k = W.shape[1]
        out = np.zeros((self.n, k))
        if not len(self.indices):
            return out
        prod = W[self.indices] * self.data[:, None]
        nonempty = np.diff(self.indptr) > 0
        out[nonempty] = np.add.reduceat(prod, self.indptr[:-1][nonempty], axis=0)
        return out

    def tdot(self, G: np.ndarray) -> np.ndarray:
        """X.T @ G, G of shape (n, k)."""
        vals = G[self.row_of] * self.data[:, None]
        return np.stack([np.bincount(self.indices, weights=vals[:, j], minlength=self.dim)
                         for j in range(G.shape[1])], axis=1)


def _softmax(z):
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -35, 35)))


def _lbfgs(fun, x0, max_iter=150, m=10, tol=1e-5):
    """Minimises fun(x) -> (value, gradient). Standard two-loop L-BFGS with
    a backtracking (Armijo) line search."""
    x = x0.copy()
    f, g = fun(x)
    s_hist, y_hist = [], []
    for it in range(max_iter):
        gnorm = np.linalg.norm(g)
        if gnorm < tol:
            break
        q = g.copy()
        alphas = []
        for s, y in reversed(list(zip(s_hist, y_hist))):
            rho = 1.0 / (y @ s)
            a = rho * (s @ q)
            alphas.append((a, rho, s, y))
            q -= a * y
        if y_hist:
            s, y = s_hist[-1], y_hist[-1]
            q *= (s @ y) / (y @ y)
        else:
            q /= max(gnorm, 1.0)
        for a, rho, s, y in reversed(alphas):
            b = rho * (y @ q)
            q += s * (a - b)
        d = -q
        slope = g @ d
        if slope >= 0:          # not a descent direction: restart from the gradient
            s_hist, y_hist = [], []
            d = -g / max(gnorm, 1.0)
            slope = g @ d
        step = 1.0
        while True:
            x_new = x + step * d
            f_new, g_new = fun(x_new)
            if f_new <= f + 1e-4 * step * slope or step < 1e-10:
                break
            step *= 0.5
        s, y = x_new - x, g_new - g
        if s @ y > 1e-10:
            s_hist.append(s)
            y_hist.append(y)
            if len(s_hist) > m:
                s_hist.pop(0)
                y_hist.pop(0)
        converged = abs(f - f_new) <= 1e-7 * max(1.0, abs(f))
        x, f, g = x_new, f_new, g_new
        if converged:
            break
    return x


def train_softmax(X: Sparse, y: np.ndarray, n_classes: int, weights=None, l2: float = 1.0,
                  max_iter: int = 150) -> np.ndarray:
    """Multinomial logistic regression. Returns W, shape (dim, n_classes)."""
    n = X.n
    w = np.ones(n) if weights is None else np.asarray(weights, dtype=np.float64)
    Y = np.zeros((n, n_classes))
    Y[np.arange(n), y] = 1.0
    total = w.sum()

    def fun(flat):
        W = flat.reshape(X.dim, n_classes)
        P = _softmax(X.dot(W))
        loss = -(w * np.log(P[np.arange(n), y] + 1e-12)).sum() / total + 0.5 * l2 * (flat @ flat) / total
        G = X.tdot((P - Y) * w[:, None]) / total + l2 * W / total
        return loss, G.ravel()

    return _lbfgs(fun, np.zeros(X.dim * n_classes), max_iter=max_iter).reshape(X.dim, n_classes)


def predict_softmax(X: Sparse, W: np.ndarray) -> np.ndarray:
    return _softmax(X.dot(W))


def train_sigmoid(X: Sparse, Y: np.ndarray, l2: float = 1.0, max_iter: int = 150,
                  max_pos_weight: float = 5.0) -> np.ndarray:
    """One logistic regression per column of Y (0/1), trained together.
    A tag on few recipes has its positives weighted up (negatives/positives,
    at most max_pos_weight), or it would rarely score above one half.
    Returns W, shape (dim, k)."""
    n, k = Y.shape
    pos = Y.sum(axis=0)
    pw = np.clip((n - pos) / np.maximum(pos, 1), 1.0, max_pos_weight)
    Wt = np.where(Y > 0, pw[None, :], 1.0)
    total = Wt.sum(axis=0).mean()

    def fun(flat):
        W = flat.reshape(X.dim, k)
        P = _sigmoid(X.dot(W))
        loss = -(Wt * (Y * np.log(P + 1e-12) + (1 - Y) * np.log(1 - P + 1e-12))).sum() / total \
            + 0.5 * l2 * (flat @ flat) / total
        G = X.tdot(Wt * (P - Y)) / total + l2 * W / total
        return loss, G.ravel()

    return _lbfgs(fun, np.zeros(X.dim * k), max_iter=max_iter).reshape(X.dim, k)


def predict_sigmoid(X: Sparse, W: np.ndarray) -> np.ndarray:
    return _sigmoid(X.dot(W))


def viterbi(log_emit: np.ndarray, log_trans: np.ndarray, log_start: np.ndarray) -> list[int]:
    """Most likely label sequence. log_emit (n, k), log_trans (k, k) with
    [from, to], log_start (k,)."""
    n, k = log_emit.shape
    if n == 0:
        return []
    score = log_start + log_emit[0]
    back = np.zeros((n, k), dtype=np.int64)
    for i in range(1, n):
        cand = score[:, None] + log_trans
        back[i] = cand.argmax(axis=0)
        score = cand.max(axis=0) + log_emit[i]
    path = [int(score.argmax())]
    for i in range(n - 1, 0, -1):
        path.append(int(back[i, path[-1]]))
    return path[::-1]


def grouped_folds(groups: list, k: int, seed: int = 0) -> list[np.ndarray]:
    """k folds of row indices, every group (recipe) wholly in one fold."""
    uniq = sorted(set(groups))
    rng = np.random.default_rng(seed)
    rng.shuffle(uniq)
    fold_of = {g: i % k for i, g in enumerate(uniq)}
    groups = np.asarray([fold_of[g] for g in groups])
    return [np.nonzero(groups == f)[0] for f in range(k)]
