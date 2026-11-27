# Third-party sources and permissions

This file is a provenance/permission checklist. It does not grant a project-wide license or certify that the inspected materials may be redistributed.

## Inspiration references

The project authors identify the following publications/projects as inspiration:

- ChatMOF — [official repository](https://github.com/Yeonghun1675/ChatMOF).
- Inverting the structure-property map of truss metamaterials by deep learning — [official InvertibleTrussDesign repository](https://github.com/jhbastek/InvertibleTrussDesign).

Inspiration and actual code derivation are different relationships. A license in either reference repository applies to its covered material; it must not be assumed to cover the supplied inverse-design module, supplied conformal-design module, pretrained weights, database or anatomical meshes in this project.

No files from those reference repositories are newly incorporated merely by listing them here. If actual copied/derived code is identified, record its original repository and commit/version, covered files, applicable license text/notices and local modifications.

## Components awaiting provenance/rights confirmation

| Component | Scope | Required decision |
| --- | --- | --- |
| BoneMetGPT integration code | Product workflow, service wrappers, adapters, UI and release support | Confirm authors/rights holder and approved project license. |
| Supplied inverse-design module A | source/TrussInvertibleDesign/ | Confirm actual provider/source/version, modification history and redistribution permission; do not infer ownership from the similar inspiration repository name. |
| Supplied conformal-design module B | source/smart_lattice_conformal_design/ | Confirm actual provider/source/version, third-party portions and redistribution permission. |
| Pretrained inverse/forward weights and scalers | Declared matched inference resources | Confirm provenance, permitted use and distribution conditions independently of Python-source licensing. |
| Formal data and query database | Structure–stiffness data and derived query projection | Confirm ownership, underlying data rights and permitted redistribution of derivatives. |
| Real-case meshes, outputs and scientific images | Anatomical inputs, historical cases and figures | Confirm provider, permitted public use and privacy/disclosure restrictions. |
| Runtime libraries | Python/Node dependencies | Retain required upstream license/notices for dependencies actually shipped. Installed environments and dependency trees are not source-archive contents by default. |

Original supplier archives and historical evidence remain internal. A candidate archive may be prepared locally for review; it must not be redistributed until the relevant rights are resolved. The public export must exclude unresolved material or provide an explicitly approved access arrangement. Internal test access is not public redistribution authorization.

The source ZIP must exclude repository history and local/private files. A future hosted repository needs both a delivered-file review and a history-sensitive-information review; no Git history rewrite or upload is authorized by this notice.
