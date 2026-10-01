variable "region" {
  type        = string
  description = "AWS region for Glue and Athena."
}

variable "lab_account_id" {
  type        = string
  description = "Account allowed to own the catalog and workgroup."
}

variable "project_prefix" {
  type        = string
  description = "Unique resource prefix for this implementation."
}

variable "raw_s3_uri" {
  type        = string
  description = "Existing raw CSV prefix; read only."
}

variable "output_bucket" {
  type        = string
  description = "Existing bucket owned by this new project."
}

variable "curated_version" {
  type        = string
  description = "Version directory for the curated Parquet."
}
