# BoneMetGPT · Lattice Design for Bone Implants

Start with a stiffness target, choose a lattice candidate, and map it to your input mesh to generate traceable geometry.

## Workflow

Review the target → confirm generation of three candidates → compare predictions, errors, and eligibility, then confirm a candidate → review mesh requirements and upload INP → confirm the selected domain and check quality → confirm computation → view Job results.

Ask for reference data in natural language, or upload a UTF-8 CSV with one row and these nine columns: `C11, C12, C13, C22, C23, C33, C44, C55, C66`. Target stiffness values are in GPa; the file size limit is 5 MB. XLSX, multiple target rows, and legacy `full_pred` tables are not supported. CSV parsing does not use an LLM.

Meshes use mm and the original coordinate system. The computational domain consists of explicitly selected C3D8/C3D8R hexahedra; excluded regions do not participate. Confirming the selected domain and starting computation are separate actions. Reopening a Job restores its saved state and evidence; it does not resume computation.

## Interpreting results

B31 is an engineering beam mesh; STL/PNG provide geometry previews. Candidate ranking and successful geometry generation do not establish mechanical, FEA, manufacturing, or clinical validation. `NOT_FULL_DUAL_SPACE_FUSION`. Failure records are retained; continue only as allowed by the recorded Job state.
