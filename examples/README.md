# Examples

## Reproduce the design example

Use CPython 3.10 and the CPU dependencies pinned in the Windows installation package. If you have set up that package, use the same Python interpreter here. Use its `release/artifacts_v1` directory, or another trusted directory with the four file paths listed in the [resource inventory](../resources/manifest.json).

~~~powershell
python -I -B examples/run_design.py --resources '<artifact root>' --output inverse-results
python -I -B examples/run_design.py --resources '<artifact root>' --mesh examples/unit_cube.inp --select-rank 1 --whole-mesh-mm --output design-results
~~~

The first command generates three candidates. Inspect the ranking in `inverse-results/design.json`. The second command explicitly chooses an eligible rank, confirms the whole file as the design domain in millimetres, and exports the conformal B31 INP and an STL preview. Both commands produce `candidates.csv` and a `design.json` report with input, model and output hashes. They require a new output directory.

The bundled target has nine stiffness values in GPa, and the bundled mesh has one C3D8 element. To use a different target, supply `--target` with a one-row CSV of the same format. The mesh argument accepts a whole-file C3D8 or C3D8R domain that passes V1's mesh check. The selected rank and physical rod diameter are recorded in the report. The example uses a fixed seed and the V1 optimization, candidate ranking, geometry adaptation and conformal kernel.

## Synthetic read-only database query

Use Python 3.10 and NumPy. Run the following from the repository root.

~~~powershell
python -m pip install numpy==1.26.4
python -I -B examples/query_demo.py
~~~

The CSV contains 24 constructed records with fictional identifiers and values. Relative density and diameter are dimensionless. Stiffness is normalized as C/Eb. Eligibility flags are assigned for demonstration.

The script builds a temporary SQLite projection using V1's projection builder and runs the V1 read-only query service. It removes the temporary database and manifest on exit.

| Operation | Expected result |
| --- | --- |
| Overview | 24 eligible synthetic records |
| Count with relative density below 0.20 | 8 |
| Top three by normalized C11 with relative density below 0.20 | Sample IDs 900008, 900007, 900006 |
| Records with relative density below 0.01 | An empty list |

The Windows package's separate research subset contains 10,000 eligible records from the formal query projection. The two examples have different datasets and result counts.

