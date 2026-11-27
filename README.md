# BoneMetGPT V1.0.0

BoneMetGPT is research software for stiffness-guided lattice design. It connects a nine-component stiffness target, inverse candidate generation, surrogate ranking, hexahedral mesh inspection, conformal beam generation, and a results viewer.

The source snapshot follows the maintained V1 baseline after the four-stage update. It includes direct result cards for ordinary database queries and the shortened chat homepage introduction. The inverse-design workflow uses latent-space optimization and NMSE ranking.

## Full workflow with the public research subset

The Windows installation package includes the application, a stratified **10,000-record** query subset, and the four matching pretrained model and scaler files. Database searches and counts apply to the subset. Package setup consumes the pinned Python and Node dependencies and generates the installation identity. Copy the provided [configuration template](.env.example) to source/.env and configure your own API key locally for the chat interface.

Use the installation package and its separately published SHA-256 when the release is available. The package includes its [setup script](setup_public.ps1), instructions, sampling metadata and bundled design inputs. For local review, the prepared archive and checksums are under `.local/`. The [resource guide](resources/README.md) explains the dataset and model identities.

## Reproduce a design without a chat API

The [command-line design example](examples/README.md#reproduce-the-design-example) takes a stiffness CSV and an optional INP mesh, uses the matching models and scalers, and exports the ranked candidates and conformal geometry. It uses CPU inference and a fixed seed.

## Try the synthetic database query

With Python 3.10 and NumPy installed, run `python -I -B examples/query_demo.py`. This builds a temporary SQLite projection from 24 generated records. See [expected results](examples/README.md#synthetic-read-only-database-query).

The source is under [source/](source/). [Third-party notices](THIRD_PARTY_NOTICES.md) record attribution and provenance. Surrogate predictions and generated geometry support computational reproducibility. Mechanical, manufacturing and clinical validation require separate evidence.

