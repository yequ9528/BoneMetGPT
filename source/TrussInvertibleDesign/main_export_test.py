import os
import json
import time

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import numpy as np
import pandas as pd
import torch

try:
    from .train_parameters import C_names, Point_names
    from .src.inference_contract import (
        apply_inference_contract,
        load_reproduction_profile_from_environment,
    )
    from .src.loadDataset import getNormalization
    from .src.model_utils import InverseCVAE
except ImportError:
    from train_parameters import C_names, Point_names
    from src.inference_contract import (
        apply_inference_contract,
        load_reproduction_profile_from_environment,
    )
    from src.loadDataset import getNormalization
    from src.model_utils import InverseCVAE


E_TC4 = 110.0
NUM_PER_STIFFNESS = 3


def generate_direct_structure(
    c_target_norm,
    *,
    inverse_model,
    scaler_x,
    generator,
):
    with torch.no_grad():
        x_norm, _, _ = inverse_model(c_target_norm, generator=generator)
        x_gen = scaler_x.unnormalize(x_norm.cpu()).numpy()
        return np.clip(x_gen, 0, 1)


def main() -> None:
    profile = load_reproduction_profile_from_environment()
    generator = apply_inference_contract(profile)
    device = torch.device("cpu")

    inverse_model = torch.load(
        "models/inverse_cvae150w.pt",
        map_location=device,
        weights_only=False,
    ).eval()
    forward_model = torch.load(
        "models/forward_stiffness150w.pt",
        map_location=device,
        weights_only=False,
    ).eval()
    scaler_x, scaler_y = getNormalization()

    algorithm = os.environ.get('LATTICEGPT_INVERSE_ALGORITHM', 'truss_direct_legacy')
    if algorithm == 'truss_latent_20261110':
        run_optimized(inverse_model, forward_model, scaler_x, scaler_y, generator)
        return
    if algorithm != 'truss_direct_legacy':
        raise ValueError('unsupported inverse algorithm')

    df_target = pd.read_csv("datatest/target_stiffness.csv")
    c_true = df_target[C_names].values
    c_normalized = c_true / E_TC4
    c_target = torch.tensor(c_normalized, dtype=torch.float32)
    c_target_norm = scaler_y.normalize(c_target).to(device)

    generated_results = []
    predicted_stiffness_list = []
    for index in range(len(c_target_norm)):
        c_single = c_target_norm[index : index + 1]
        for _candidate_order in range(NUM_PER_STIFFNESS):
            structure = generate_direct_structure(
                c_single,
                inverse_model=inverse_model,
                scaler_x=scaler_x,
                generator=generator,
            )
            generated_results.append(structure)
            with torch.no_grad():
                x_tensor = torch.tensor(structure, dtype=torch.float32)
                x_norm = scaler_x.normalize(x_tensor).to(device)
                pred_norm = forward_model(x_norm)
                pred = scaler_y.unnormalize(pred_norm.cpu()).numpy()
                predicted_stiffness_list.append((pred * E_TC4).flatten())

    df_struct = pd.DataFrame(np.vstack(generated_results), columns=Point_names)
    df_stiffness = pd.DataFrame(np.vstack(predicted_stiffness_list), columns=C_names)
    pd.concat([df_struct, df_stiffness], axis=1).to_csv(
        "datatest/prediction_target_test.csv",
        index=False,
        encoding="utf-8-sig",
    )


def run_optimized(inverse_model, forward_model, scaler_x, scaler_y, generator):
    try:
        from .src.latent_optimization import optimize_one, VERSION
    except ImportError:
        from src.latent_optimization import optimize_one, VERSION
    started = time.perf_counter()
    rows, diagnostics = [], []
    for index, target in enumerate(pd.read_csv('datatest/target_stiffness.csv')[C_names].values, 1):
        picked, threshold = optimize_one(target, inverse_model=inverse_model,
            forward_model=forward_model, scaler_x=scaler_x, scaler_y=scaler_y, generator=generator)
        for theta, prediction, nmse in picked:
            rows.append([index, nmse, *theta, *prediction])
        diagnostics.append({'target_index': index, 'diversity_threshold': threshold,
                            'diversity_fallback': threshold == 0.})
    pd.DataFrame(rows, columns=['target_id', 'nmse', *Point_names, *C_names]).to_csv(
        'datatest/prediction_target_test.csv', index=False, encoding='utf-8-sig')
    metadata = {'schema_version': 'truss_inverse_metadata_v1',
                'algorithm': {'version': VERSION, 'prediction_kind': 'forward_surrogate'},
                'diagnostics': diagnostics, 'elapsed_ms': int((time.perf_counter()-started)*1000)}
    with open('datatest/inverse_metadata.json', 'w', encoding='utf-8') as stream:
        json.dump(metadata, stream, allow_nan=False, sort_keys=True)


if __name__ == "__main__":
    main()
