"""V1 click model (LightGBM + factorization machine) scored from its exported files with numpy + LightGBM.

Mirrors src/portable.py in the model repo. On load it re-scores the release's golden sample and refuses
to start if any prediction differs from V1's by more than the published tolerance, so serving provably
scores exactly like the trained model.

A row is a dict of raw feature values keyed by the model's input columns (manifest["input_columns"]);
missing keys and None mean "missing": unseen category, or no history yet.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
from numpy.typing import NDArray

Row = Mapping[str, Any]
FloatArray = NDArray[np.float64]


def logit(p: NDArray[Any]) -> FloatArray:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.asarray(np.log(p / (1 - p)), dtype="float64")


def _lookup(spec: Mapping[str, list[Any]]) -> dict[Any, int]:
    return dict(zip(spec["values"], spec["codes"], strict=True))


def _number(value: Any) -> float:
    return float("nan") if value is None else float(value)


@dataclass(frozen=True)
class CTRPrediction:
    p_lightgbm: FloatArray
    p_fm: FloatArray

    @property
    def p_v1(self) -> FloatArray:
        """V1: the two models' log-odds averaged, back to a probability."""
        return 1 / (1 + np.exp(-self.z))

    @property
    def z(self) -> FloatArray:
        return (logit(self.p_lightgbm) + logit(self.p_fm)) / 2

    @property
    def uncertainty(self) -> FloatArray:
        """Half the two models' disagreement in log-odds: the ranker's uncertainty band."""
        return np.abs(logit(self.p_lightgbm) - logit(self.p_fm)) / 2


class _NumpyFM:
    """Factorization machine forward pass on exported weights (same math as the model repo's src/serving.py)."""

    def __init__(self, path: Path) -> None:
        w = np.load(path)
        self.embedding, self.linear = w["embedding"], w["linear"]
        self.dense_embedding, self.dense_linear = w["dense_embedding"], w["dense_linear"]
        self.bias = float(w["bias"][0])
        self.sizes, self.offsets = w["sizes"], w["offsets"]

    def predict(self, idx: NDArray[np.int32], dense: NDArray[np.float32]) -> FloatArray:
        ids = np.minimum(idx, self.sizes - 1) + self.offsets
        v = np.concatenate([self.embedding[ids], dense[:, :, None] * self.dense_embedding[None]], axis=1)
        z = (
            self.bias
            + self.linear[ids].sum(1)
            + (dense * self.dense_linear).sum(1)
            + 0.5 * (v.sum(1) ** 2 - (v**2).sum(1)).sum(1)
        )
        return np.asarray(1 / (1 + np.exp(-z)), dtype="float64")


class CTRModel:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.manifest = json.loads((directory / "manifest.json").read_text())
        enc = json.loads((directory / "encoders.json").read_text())
        self._booster = lgb.Booster(model_file=str(directory / "lightgbm.txt"))
        self._fm = _NumpyFM(directory / "fm_weights.npz")

        trees = enc["lightgbm"]
        self._categorical: list[str] = trees["categorical"]
        self._passthrough: list[str] = trees["flags"] + trees["numeric"]
        self._codes = {col: _lookup(spec) for col, spec in trees["codes"].items()}
        self._freq = {
            col: (_lookup(spec["counts"]), np.asarray(spec["decile_edges"]))
            for col, spec in trees["frequency_deciles"].items()
        }

        fm = enc["factorization_machine"]
        self._fm_codes = {col: _lookup(spec) for col, spec in fm["codes"].items()}
        self._fm_flags: list[str] = fm["flags"]
        self._fm_bins = {col: np.asarray(edges) for col, edges in fm["bin_edges"].items()}
        self._fm_dense: list[str] = fm["dense"]
        self._fm_mean, self._fm_std = np.asarray(fm["dense_mean"]), np.asarray(fm["dense_std"])

    def known_values(self, column: str) -> list[Any]:
        """Category values the model learned (seen often enough in training to get their own code)."""
        return list(self._codes[column])

    @property
    def input_columns(self) -> list[str]:
        columns: list[str] = self.manifest["input_columns"]
        return columns

    def predict(self, rows: Sequence[Row]) -> CTRPrediction:
        p_lightgbm = np.asarray(self._booster.predict(self._lightgbm_matrix(rows)), dtype="float64")
        return CTRPrediction(p_lightgbm=p_lightgbm, p_fm=self._fm.predict(*self._fm_arrays(rows)))

    def verify_golden_sample(self) -> float:
        """Re-score the release's golden rows; raise if any prediction drifts past the tolerance."""
        golden = json.loads((self.directory / "golden_sample.json").read_text())
        prediction = self.predict(golden["rows"])
        diff = max(
            float(np.abs(prediction.p_lightgbm - np.asarray(golden["p_lightgbm"])).max()),
            float(np.abs(prediction.p_fm - np.asarray(golden["p_fm"])).max()),
            float(np.abs(prediction.p_v1 - np.asarray(golden["p_v1"])).max()),
        )
        if diff > golden["tolerance"]:
            raise RuntimeError(f"CTR model disagrees with the trained V1 by {diff:.2e} on its golden sample")
        return diff

    def _lightgbm_matrix(self, rows: Sequence[Row]) -> FloatArray:
        out = []
        for row in rows:
            values: list[float] = [self._codes[col].get(row.get(col), 0) for col in self._categorical]
            for col, (counts, edges) in self._freq.items():
                freq = counts.get(row.get(col), 0)
                values.append(0 if freq == 0 else int(np.searchsorted(edges, freq, side="right")) + 1)
            values += [_number(row.get(col)) for col in self._passthrough]
            out.append(values)
        return np.asarray(out, dtype="float64")

    def _fm_arrays(self, rows: Sequence[Row]) -> tuple[NDArray[np.int32], NDArray[np.float32]]:
        idx, dense = [], []
        for row in rows:
            ids = [codes.get(row.get(col), 0) for col, codes in self._fm_codes.items()]
            ids += [int(row.get(col) or 0) for col in self._fm_flags]
            for col, edges in self._fm_bins.items():
                value = _number(row.get(col))
                ids.append(0 if np.isnan(value) else 1 if value == 0 else int(np.searchsorted(edges, value, side="right")) + 2)
            idx.append(ids)
            dense.append([_number(row.get(col)) for col in self._fm_dense])
        rates = (logit(np.asarray(dense, dtype="float64")) - self._fm_mean) / self._fm_std
        return np.asarray(idx, dtype="int32"), np.clip(np.nan_to_num(rates), -5, 5).astype("float32")
