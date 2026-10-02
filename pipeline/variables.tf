variable "region" {
  type = string
}
variable "lab_account_id" {
  type = string
}
variable "project_prefix" {
  type = string
}
variable "output_bucket" {
  type = string
}
variable "curated_version" {
  type = string
}
variable "offline_store_bucket" {
  type        = string
  description = "Existing Learner Lab SageMaker bucket for offline features."
}
variable "feature_version" {
  type = string
}
variable "preprocessing_contract" {
  type = string
}
variable "model_version" {
  type = string
}
variable "execution_role_name" {
  type        = string
  description = "Existing SageMaker execution role; use LabRole in Learner Lab."
}
variable "xgboost_image" {
  type        = string
  description = "Same AWS XGBoost 1.7 image for processing, training, evaluation and registered inference."
}
variable "instance_type" {
  type = string
}
variable "auc_threshold" {
  type = number
  validation {
    condition     = var.auc_threshold >= 0 && var.auc_threshold <= 1
    error_message = "AUC threshold must be between 0 and 1."
  }
}
