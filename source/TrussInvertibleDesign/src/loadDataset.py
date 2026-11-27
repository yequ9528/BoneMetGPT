import torch
import pandas as pd
import os
from torch.utils.data import TensorDataset, random_split
try:
    from .normalization import MinMaxScaler
    from ..train_parameters import Point_names, C_names, dataPath
except ImportError:
    from src.normalization import MinMaxScaler
    from train_parameters import Point_names, C_names, dataPath

SCALER_DIR = "scalers/"

def getNormalization(force_recalculate=False):
    policy = os.getenv("LATTICEGPT_SCALER_POLICY", "training")
    if policy not in {"training", "require_existing"}:
        raise ValueError(f"unsupported scaler policy: {policy}")
    scaler_x_path = f"{SCALER_DIR}/scaler_X.pt"
    scaler_y_path = f"{SCALER_DIR}/scaler_Y.pt"
    if policy == "require_existing":
        if force_recalculate:
            raise RuntimeError("scaler recalculation is forbidden in formal product runtime")
        missing = [
            path
            for path in (scaler_x_path, scaler_y_path)
            if not os.path.exists(path)
        ]
        if missing:
            raise FileNotFoundError(
                "formal product runtime requires existing scalers: " + ", ".join(missing)
            )

    scaler_X = MinMaxScaler()
    scaler_Y = MinMaxScaler()

    # 加载保存好的归一化
    if (
        os.path.exists(scaler_x_path)
        and os.path.exists(scaler_y_path)
        and not force_recalculate
    ):
        scaler_X.load(scaler_x_path)
        scaler_Y.load(scaler_y_path)
        return scaler_X, scaler_Y

    # ✅ 使用统一的 datapath
    df = pd.read_csv(dataPath)
    X = torch.tensor(df[Point_names].values, dtype=torch.float32)
    Y = torch.tensor(df[C_names].values, dtype=torch.float32)

    scaler_X.fit(X)
    scaler_Y.fit(Y)

    # 保存
    scaler_X.save(scaler_x_path)
    scaler_Y.save(scaler_y_path)
    return scaler_X, scaler_Y

def getDataset(scaler_X, scaler_Y):
    # ✅ 使用统一的 datapath
    df = pd.read_csv(dataPath)
    X = torch.tensor(df[Point_names].values, dtype=torch.float32)
    Y = torch.tensor(df[C_names].values, dtype=torch.float32)

    X_norm = scaler_X.normalize(X)
    Y_norm = scaler_Y.normalize(Y)

    dataset = TensorDataset(X_norm, Y_norm)
    train_size = int(0.9 * len(dataset))
    test_size = len(dataset) - train_size
    return random_split(dataset, [train_size, test_size])
