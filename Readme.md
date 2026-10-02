# Customer churn pipeline

Three stages are implemented: **data-catalog**, **preprocessing + feature-store**,
and the **SageMaker pipeline**. Stages 2 and 3 have been authored but not run.
Batch inference, manual approval automation and monitoring are not implemented.

## Configuration: one file, two consumers

`.env` contains the settings. It contains no AWS keys; `AWS_PROFILE` selects the
credentials in your local AWS profile. `.env.example` is the committed example.
Neither Python nor Terraform automatically loads this file. From the repository
root, explicitly load it into your terminal before running either tool:

```bash
set -a
source .env
set +a
```

`set -a` exports the assignments so child processes can read them. `source`
loads the values, and `set +a` turns automatic exporting back off. Repeat this
after editing `.env` or opening another terminal.

| Setting in `.env` | Terraform reads | Python reads through `config.py` |
|---|---|---|
| `AWS_PROFILE` | AWS provider's profile selection | `AWS_PROFILE` |
| `TF_VAR_region` | `var.region` | `REGION` |
| `TF_VAR_lab_account_id` | `var.lab_account_id` | `LAB_ACCOUNT_ID` |
| `TF_VAR_project_prefix` | `var.project_prefix` | `PROJECT_PREFIX` |
| `TF_VAR_raw_s3_uri` | `var.raw_s3_uri` | `RAW_S3_URI` |
| `TF_VAR_output_bucket` | `var.output_bucket` | `OUTPUT_BUCKET` |
| `TF_VAR_curated_version` | `var.curated_version` | `CURATED_VERSION` |

`TF_VAR_` is Terraform's [native environment-variable convention](https://developer.hashicorp.com/terraform/cli/config/environment-variables).
Terraform does **not** read `config.py`. Python reads the same exported values;
`config.py` does not load another file or supply fallback values. Missing values
cause an error. There is no `terraform.tfvars` in this implementation. Do not add
one or use `-var` overrides: Terraform would then receive values Python does not.

Names are derived, not separately configured:

- Database: project prefix with hyphens replaced by underscores → `churn_rebuild`.
- Workgroup: project prefix unchanged → `churn-rebuild`.
- Parquet location: output bucket + `curated/cell2cell/` + curated version.
- Pipeline: project prefix + `-pipeline` → `churn-rebuild-pipeline`.
- Feature Group: project prefix + `-features-` + feature version.
- Model package group: project prefix + `-models`.

These simple expressions appear in Terraform and `config.py`; their input values
exist only in `.env`. Table names and the dataset's column schema are fixed in
code, not environment settings. Use a lowercase project prefix with letters,
numbers, and hyphens; use a simple version such as `v1`.

## Data-catalog

### What each command does

1. **Terraform** creates a Glue database, a raw CSV table, and an Athena workgroup
   in the Learner Lab account. It does not copy data or run SQL.
2. **Python** submits two SQL statements to Athena, in order:
   - `01_create_curated.sql`: reads the raw CSVs, converts column types, and writes
     Snappy Parquet partitioned into labeled `train` and unlabeled `holdout`.
     This `train` partition is the labeled pool; train/validation/test splitting
     belongs to preprocessing later. Non-CSV files and nonnumeric customer IDs
     are excluded; headers are skipped by the raw table.
   - `02_create_view.sql`: creates a view of labeled rows excluding
     `retentioncalls`, `retentionoffersaccepted`, and `madecalltoretentionteam`.
     The curated Parquet itself retains those columns.

Athena registers `cell2cell_curated` and `vw_cell2cell_training` in the Glue
database when these SQL statements run. They are SQL-created objects, not
separately managed Terraform resources.

### Locations

| Purpose | Location |
|---|---|
| Raw input, read only | `s3://aai-540-group-2/raw/cell2cell/` |
| Curated output | `s3://customer-churn-pipeline-a81b79/curated/cell2cell/v1/` |
| Athena query results | `s3://customer-churn-pipeline-a81b79/athena-results/` |
| Glue database | `churn_rebuild` in lab account `702238036392`, region `us-east-1` |
| Athena workgroup | `churn-rebuild` in that same account and region |

The bucket already exists in your main account. This stage uses it but does not
manage it or the team bucket. Terraform state stays local in `data-catalog/` and
is ignored by Git. No old Terraform state is copied into this project.

### Run from the repository root

The local `.env` is already populated. On another checkout, copy `.env.example`
to `.env` and adjust it first. Refresh your Learner Lab credentials as needed.

```bash
set -a
source .env
set +a

uv sync
terraform -chdir=data-catalog init
terraform -chdir=data-catalog plan
terraform -chdir=data-catalog apply
uv run python data-catalog/run_curation.py
```

Review the plan before applying. The Python command runs real Athena queries
and writes real data. It prints the query IDs so you can find the executions in
Athena. In the Athena console select workgroup `churn-rebuild`, catalog
`AwsDataCatalog`, and database `churn_rebuild` to see the tables and view.

### Running again

Terraform can be applied again normally. Curation is a one-time creation for
this table and S3 version: Athena requires the destination to be empty and the
curated table not to exist. The script deliberately has no deletion or recreate
mode. If a query fails, inspect its reason before retrying; it may have left
partial output. Changing `curated_version` alone does not remove an existing
table. If only view creation failed, its SQL can be rerun in Athena after
replacing `${database}` with `churn_rebuild`.

No self-tests, smoke tests, test suite, or shell wrappers are included.

## Preprocessing, Feature Store and pipeline

There is one execution path:

```text
curated labeled Parquet
  → Preprocess → IngestFeatures → Train → Evaluate → QualityGate
                                                      ├─ RegisterModel: PendingManualApproval
                                                      └─ Fail: no package
```

`pipeline/` contains the preprocessing and ingestion code, the Feature Group
schema, and the Terraform resources for **both stages**. It creates the Feature
Group, model package group and pipeline together. It uses the existing `LabRole`
and existing buckets; it creates no bucket, IAM role or endpoint. The
data-catalog stack remains separate.

Preprocessing splits labeled customers 70/15/15, fits medians and encodings on
training rows only, and writes label-first CSVs and fitted parameters. Ingestion
sends all three splits to the offline Feature Store. Training waits for ingestion
to succeed, then reads the **same CSV outputs directly**, not the offline store.
The unlabeled holdout is left for future inference work.

### Additional configuration

These values are in `.env` and `.env.example`. Terraform reads their `TF_VAR_`
names directly. It passes the relevant versions, region and Feature Group name
as arguments to the AWS scripts. Those scripts do not load `.env` or import
local `config.py`. `config.py` supplies the local launcher's profile, region,
account and derived pipeline name. No second settings file or fallback values
are used.

| `.env` variable | Consumer / purpose |
|---|---|
| `TF_VAR_offline_store_bucket` | Existing lab SageMaker bucket; offline Feature Group destination |
| `TF_VAR_feature_version` | Feature Group name and offline prefix |
| `TF_VAR_preprocessing_contract` | Version recorded in each run's fitted preprocessing contract |
| `TF_VAR_model_version` | Model artifact output prefix and package metadata |
| `TF_VAR_execution_role_name` | Existing role, `LabRole` |
| `TF_VAR_xgboost_image` | One image for processing, training, evaluation and registered inference |
| `TF_VAR_instance_type` | Processing and training compute, `ml.m5.xlarge` |
| `TF_VAR_auc_threshold` | Minimum test ROC-AUC for registration, `0.60` |

The example image URI is for us-east-1 and XGBoost 1.7. The jobs use its bundled
Python libraries; no pip installation is performed inside jobs. The local
NumPy/Pandas/PyArrow dependencies support schema regeneration, not local training.
Changing a setting requires reloading `.env` and reapplying Terraform to update
the registered definition. The launcher has no overrides that silently diverge
from it.

### Provision, then run

After data-catalog curation has succeeded, from the repository root:

```bash
set -a
source .env
set +a
uv sync
terraform -chdir=pipeline init
terraform -chdir=pipeline plan
terraform -chdir=pipeline apply
uv run python pipeline/run_pipeline.py
```

Review the plan. Terraform uploads four Python files to `pipeline/code/` and
registers `definition.tf` as JSON. There is no separate upload command, Python
definition generator, or JSON template to keep in sync. Do not apply code changes
while an execution is running: its later steps read this same code prefix.

The launcher returns an execution ARN and exits; AWS continues running the jobs.
For another run, repeat only the last command. Open SageMaker Studio → Pipelines
→ `churn-rebuild-pipeline` to follow it. Candidates appear in model package group
`churn-rebuild-models`. Registration does not approve or deploy a model.

The execution identity needs to pass `LabRole`; that role needs access to the
personal output bucket and existing lab SageMaker bucket. The offline bucket in
`.env` must already exist. Refresh Learner Lab credentials before running.

### Outputs and versions

- Personal bucket: `pipeline/{execution-id}/process/` contains splits, identifiers
  and the fitted preprocessing contract; `feature-store/ingestion.json` contains
  accepted-record counts.
- Existing lab bucket: `features/{project-prefix}/cell2cell/{feature-version}/`
  receives offline records under AWS-generated subfolders. Delivery is asynchronous.
  These are rebuildable features, isolated from authoritative inputs as in the
  team implementation.
- Personal bucket: `models/cell2cell/{model-version}/{execution-id}/` contains
  the training job's model artifact and `evaluation/evaluation.json`.
- Registry: each passing run adds a numeric package version, independently of
  `MODEL_VERSION`. Package metadata includes its fitted preprocessing S3 URI.

There is no execution manifest. SageMaker records execution and step status.
Evaluation reports metrics at classification threshold **0.5**; the **AUC gate**
uses `TF_VAR_auc_threshold`. Recalibration factor **1.0** means no adjustment.
No cost-based threshold fitting or calibration is included.

### Feature schema changes

`pipeline/schema.json` starts with the team's 70 model feature names, stored
as Fractional fields, plus customer ID, label, split, event time and execution ID.
Ingestion compares it with actual processed fields before writing records.
If transformation changes alter that shape, regenerate from actual curated data:

```bash
aws s3 cp "s3://${TF_VAR_output_bucket}/curated/cell2cell/${TF_VAR_curated_version}/split=train/" data/schema-input/ --recursive
uv run python pipeline/generate_schema.py --curated-directory data/schema-input
```

Use an empty local directory when switching curated versions. Bump
`TF_VAR_feature_version`, reload `.env`, and review the Terraform plan before
applying. A changed group name replaces the group managed by this stack; old
offline S3 files are not deleted by Terraform. Regeneration is a real data
operation, not a test. It has not been run here.

