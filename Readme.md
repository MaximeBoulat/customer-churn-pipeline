# Customer churn pipeline

We are rebuilding one stage at a time. Only **data-catalog** is implemented.

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

Three names are derived, not separately configured:

- Database: project prefix with hyphens replaced by underscores → `churn_rebuild`.
- Workgroup: project prefix unchanged → `churn-rebuild`.
- Parquet location: output bucket + `curated/cell2cell/` + curated version.

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
