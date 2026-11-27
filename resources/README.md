# Research resources and access

## Windows public subset package

The Windows installation package bundles four pretrained model and scaler files and **10,000 records** from the formal read-only projection. The CSV, matching SQLite projection and projection manifest are bound together as an independent release identity. The source projection, sampling algorithm, seed, row counts and per-file SHA-256 values are documented in `resources/subset_sampling.json` inside the package.

The subset uses 1,000 randomly selected eligible records from each of ten relative-density deciles, with a fixed seed. The density deciles are defined after sorting by relative density and sample ID. The subset keeps the numeric lookup fields, replaces source IDs with independent publication-local IDs, and uses four flags implied by eligibility in the parent projection. The SQLite schema contains one table with a unique primary key and no foreign-key relationships. Data validation covers numeric ranges and the recorded eligibility checks. The parent projection has no field identifying records by test or production origin. Database searches, counts and statistics apply to this edition's subset. The full formal dataset and historical Jobs are outside the installation package.

## Command-line model files

The design example requires `models/inverse_model.pt`, `models/forward_model.pt`, `scalers/scaler_X.pt` and `scalers/scaler_Y.pt`. Point `--resources` at the package's `release/artifacts_v1` directory. The script verifies the size and SHA-256 of each file before loading it. Use files from a trusted project distribution. Older public LatticeGPT snapshots used a different inverse checkpoint.

## Formal resource inventory

[manifest.json](manifest.json) lists the seven original formal V1 resources and their hashes. The model and scaler hashes match this edition. The CSV, query SQLite and projection-manifest entries describe the complete formal resource set. The public subset package records its replacement data identities in its own bound artifact manifest. Public hosting and distribution terms for the complete formal dataset remain separate decisions.

