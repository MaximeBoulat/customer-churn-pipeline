# Customer churn pipeline

Implemented: **data-catalog**, the **SageMaker pipeline** (preprocessing, Feature
Store ingestion, training, evaluation and registration), **approval and
contract pinning**, **batch inference**, and **monitoring**. The pipeline and
batch inference have run successfully. Monitoring has not been run against AWS yet.

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
Batch inference uses the unlabeled holdout.

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
| `TF_VAR_execution_role_name` | Existing role, `LabRole`; also `config.EXECUTION_ROLE_NAME` for batch inference |
| `TF_VAR_instance_type` | Processing, training and batch compute; `config.INSTANCE_TYPE` for batch inference |
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

Review the plan. Terraform uploads the pipeline Python files to `pipeline/code/` and
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

The execution manifest is written to `pipeline/{execution-id}/execution_manifest.json`
and updated by preprocessing, ingestion and evaluation. Evaluation selects a
cost-aware cutoff on validation and applies it to test predictions; the independent
**AUC gate** uses `TF_VAR_auc_threshold`. Recalibration factor **1.0** means no
probability adjustment.

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

## Approval and contract pinning

After reviewing a candidate's evaluation report, copy its execution ARN from
SageMaker Studio → Pipelines → `churn-rebuild-pipeline`. Review the selected
cutoff, confusion matrix, expected cost versus no contact, and any top-k fallback.

From the repository root:

```bash
set -a
source .env
set +a
uv sync
uv run python approval/pin_approved_contract.py \
  --execution-arn "PASTE_PIPELINE_EXECUTION_ARN" \
  --approve \
  --note "Reviewed evaluation results; approved"
```

`--note` is optional. Omit `--approve` if the package is already approved in
Studio. The script finds the registered package and artifacts automatically;
no Terraform apply or new pipeline execution is needed.

The command pins the model, preprocessing parameters and evaluation report,
writes `approved_contract.json`, and sets the package to `Approved` when
`--approve` is supplied. Files go to:

```text
s3://customer-churn-pipeline-a81b79/models/cell2cell/approvals/
└─ churn-rebuild-models/<execution-id>/package-<version>/
   ├─ approved_contract.json
   ├─ model.tar.gz
   ├─ preprocessing_parameters.json
   └─ evaluation.json
```

The originals remain in their run folders. Identical retries are allowed;
different content cannot overwrite an existing pin. The command prints the
contract URI to give to the batch inference launcher. The selected
cutoff is in the pinned evaluation report, referenced by the contract.


## Batch inference (System 2)

Select the `approved_contract.json` URI printed by the approval command.
The pinned evaluation must contain the selected `threshold` block. The existing
pin for `v03gvl2je2xx/package-2` predates that change: apply the updated pipeline,
run it, then approve and pin the new execution before using batch inference.
Existing pins are not overwritten.

From the repository root:

```bash
set -a
source .env
set +a
uv sync
uv run python batch-transform/run_batch_inference.py \
  --contract-uri "PASTE_APPROVED_CONTRACT_S3_URI"
```

This runs a real Batch Transform job over the full curated holdout. There is no
separate Terraform apply for this stage. It uses the current AWS profile,
`TF_VAR_execution_role_name` and `TF_VAR_instance_type` through `config.py`.
The model and image come from the selected contract.

The launcher prepares inputs locally using the pinned preprocessing, starts the
AWS job, waits, applies the pinned cutoff and prints the output URI and flagged
count. Keep the terminal running until it finishes. A failed job reports its
AWS failure reason; a timeout after 30 minutes or interruption attempts to stop
the job. The temporary SageMaker Model is deleted afterward. No endpoint is created.

Outputs are written to the configured personal bucket:

```text
inference/cell2cell/<batch-run-id>/
├─ _transform/
│  ├─ input.csv
│  └─ input.csv.out
├─ scores.csv
├─ features.csv
└─ manifest.json
```

Each invocation gets a new batch run ID. `scores.csv` contains customer IDs,
raw scores, outreach flags, model/preprocessing versions and the UTC timestamp.
`features.csv` contains the prepared inputs with customer IDs. `manifest.json`
is written last; monitoring should only consume runs where it exists.
The command does not train, approve models, change the cutoff or update the
training execution manifest.


## Monitoring (System 3)

Terraform provisions CloudWatch; the Python script analyzes a
completed batch locally and writes its report and metrics. It does not launch
a SageMaker job or retrain the model.

The four monitoring settings are already in `.env` and `.env.example`:

| Setting | Initial value | Consumer |
|---|---:|---|
| `TF_VAR_monitoring_psi_threshold` | 0.2 | Terraform alarm and Python drift report |
| `TF_VAR_monitoring_f2_floor` | 0.0017 | Terraform quality alarm |
| `TF_VAR_monitoring_min_flagged` | 20 | Python group assessment and Terraform dashboard text |
| `TF_VAR_monitoring_stale_days` | 7 | Terraform freshness alarm |

Python's `monitoring/settings.py` reads the two calculation settings directly
from the exported environment, and derives the namespace from `config.py`'s
project prefix. Terraform reads those same `TF_VAR_` values. There are no tfvars
or fallback settings. The F2 floor is copied from PR #56 as a starting value;
it has **not** been calibrated to this model's performance.

From the repository root, provision the dashboard and five alarms:

```bash
set -a
source .env
set +a
uv sync
terraform -chdir=monitoring init
terraform -chdir=monitoring plan
terraform -chdir=monitoring apply
```

Then analyze a completed batch using its batch run ID, not a pipeline execution ID:

```bash
uv run python monitoring/run_monitoring.py \
  --run-id "20261003T045542Z-daaa7ef6"
```

The command reads that batch's manifest, features, scores and pinned preprocessing.
On first use of a model, it creates a baseline from that model's training CSV;
later batches reuse it. The Learner Lab profile reads the original training data.
Outputs go to:

```text
monitoring/cell2cell/
├─ baselines/<training-execution-id>/baseline.json
└─ <batch-run-id>/drift_report.json
```

Open **CloudWatch → Dashboards → churn-rebuild-monitoring** in `us-east-1`.
Metrics use namespace `churn-rebuild/ChurnMonitoring`, dimensions
`ModelName=churn-rebuild` and `Source=live` or `Source=replay`.
The detailed per-feature results are in the S3 report printed by the command.
Rerunning for the same batch updates its report and publishes another metric point.

The current holdout has no churn labels. Quality and group comparisons will be
unassessed, not successful. When actual labels are available, supply their S3 CSV
URI; the file must contain unique `customerid` values and binary `churn_label`:

```bash
uv run python monitoring/run_monitoring.py \
  --run-id "PASTE_BATCH_RUN_ID" \
  --labels "s3://YOUR_BUCKET/path/to/labels.csv"
```

The report records join coverage. Quality and group comparisons use only matching
customers and the outreach flags already written by batch inference.

### Optional labelled test replay

To exercise quality and group measurements now, reuse the approved model on its
existing test split. This runs XGBoost locally and writes a separate replay batch
to S3; it does not create new production labels or launch a Batch Transform job.
Use the same approved contract URI used for batch inference:

```bash
uv run --with 'xgboost==1.7.6' python monitoring/replay_labelled_batch.py \
  --contract-uri "PASTE_APPROVED_CONTRACT_S3_URI"
uv run python monitoring/run_monitoring.py \
  --run-id "PASTE_REPLAY_RUN_ID_PRINTED_ABOVE"
```

The replay folder contains `scores.csv`, `features.csv`, `labels.csv` and
`manifest.json`. Its manifest records the labels URI and `replay: true`, so the
monitoring command finds its labels automatically. Replay measurements appear
separately on the dashboard and do not trigger live alarms.

Monitoring is manually invoked. Terraform does not schedule it, send email
notifications, or trigger retraining. Inspect the report when an alarm fires;
missing quality data must not be interpreted as a passing quality check.
