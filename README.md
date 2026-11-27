# BoneMetGPT

**Bone-Matched Metamaterial GPT**

BoneMetGPT is an LLM-assisted research platform for the mechanics–anatomy design of bone-matched titanium lattice metamaterials. Bone stiffness varies with anatomical site and loading direction. The workflow starts with nine target stiffness components, designs candidate lattice unit cells, and maps a selected cell into a prescribed anatomical domain.

The conversational agent coordinates database queries and design tools in a multi-turn dialogue. Researchers review and confirm the target, candidate, design domain, and start of conformal generation. The saved design job links those decisions to the inputs and exported files.

## Design workflow

1. **Set the mechanical target.** Query the structure–stiffness database in natural language and use a reference record to prepare a target, or upload a CSV with nine stiffness components in GPa.
2. **Compare candidate unit cells.** Latent-space optimization with a pretrained conditional generator produces candidate geometries. A forward surrogate predicts their stiffness, and the workflow ranks the candidates by normalized mean squared error against the target. Review the predictions and choose a candidate.
3. **Generate the conformal lattice.** Import a hexahedral INP mesh, confirm the design domain, and review the mesh quality check. The conformal design tool maps the selected unit cell into that domain to produce a beam lattice.
4. **Inspect and export.** View the result in 3D, download the conformal B31 INP for downstream analysis, and export STL and PNG previews of the selected unit cell.

## Run BoneMetGPT V1

Download the Windows package and its SHA-256 files from [GitHub Releases](https://github.com/yequ9528/BoneMetGPT/releases). The package includes the application, a stratified **10,000-record structure–stiffness database subset**, the pretrained inverse and forward models with their two scalers, and example inputs. Database queries and counts use this subset. See the [research resources guide](resources/README.md) for sampling and model details.

Follow the README inside the package to install the pinned Python and Node.js dependencies. To use the chat interface, copy the [configuration template](.env.example) to `source/.env` in the extracted package and configure your own API key locally.

## Reproduce the examples

- [**Offline design example**](examples/README.md#reproduce-the-design-example). Use the packaged model weights, target stiffness CSV, and sample mesh to generate ranked candidates and export a conformal lattice on CPU.
- [**Synthetic query example**](examples/README.md#synthetic-read-only-database-query). Try the read-only database query tools on 24 synthetic records.

The application source is in [`source/`](source/). 


