variable "region" { type = string }
variable "lab_account_id" { type = string }
variable "project_prefix" { type = string }
variable "monitoring_psi_threshold" {
  type = number
  validation {
    condition     = var.monitoring_psi_threshold >= 0.1
    error_message = "PSI investigate threshold must be at least the 0.1 warning level."
  }
}
variable "monitoring_f2_floor" {
  type = number
  validation {
    condition     = var.monitoring_f2_floor >= 0 && var.monitoring_f2_floor <= 1
    error_message = "F2 floor must be between zero and one."
  }
}
variable "monitoring_min_flagged" {
  type = number
  validation {
    condition     = var.monitoring_min_flagged >= 1 && floor(var.monitoring_min_flagged) == var.monitoring_min_flagged
    error_message = "Minimum flagged count must be a positive integer."
  }
}
variable "monitoring_stale_days" {
  type = number
  validation {
    condition     = var.monitoring_stale_days >= 1 && var.monitoring_stale_days <= 7 && floor(var.monitoring_stale_days) == var.monitoring_stale_days
    error_message = "Staleness must be an integer from one to seven days."
  }
}
